"""Session — execution context coordinating Agent + State through the agentic loop.

One session = one conversation thread. Creating a session is synchronous.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent import Agent

from ..client.model import Message, ModelClient, Prompt
from ..tools.base import ToolInvocation
from ..tools.registry import ToolRegistry
from .events import (
    AUTO_COMPACT_THRESHOLD,
    COMPACTION_SYSTEM_PROMPT,
    COMPLETION_SIGNALS,
    MAX_NUDGES,
    NUDGE_MESSAGE,
    SUMMARY_PREFIX,
    AgentEvent,
    ApprovalCallback,
    ApprovalInterrupt,
    CompactResult,
    EventHandler,
    RunResult,
)
from .state import State
from .truncate import truncate_output


class Session:
    """Execution context — coordinates Agent + State through the LLM -> tools -> LLM loop.

    One session = one conversation. Creating a Session freezes the agent's registry.
    """

    def __init__(
        self,
        agent: "Agent",
        *,
        session_id: str | None = None,
        state: State | None = None,
    ) -> None:
        from .agent import Agent as AgentClass

        self._agent = agent
        self._id = session_id or str(uuid.uuid4())
        self._state = state or State()
        self._client = agent.create_client()
        self._cancelled = False
        self._session_dir: Path | None = None

        # Execution-time settings (set by callers before run())
        self.approval_callback: ApprovalCallback | None = None
        self.cancel_check: Callable[[], bool] | None = None
        self.auto_compact: bool = True
        self.context_window: int | None = None
        self.enable_nudge: bool = False

        # Mutable copy of registry (frozen from agent, but Session owns its copy)
        self._registry = agent.registry

        # Internal run state
        self._last_input_tokens: int = 0
        self._call_index: int = 0
        self._nudge_count: int = 0

    @property
    def agent(self) -> "Agent":
        return self._agent

    @property
    def state(self) -> State:
        return self._state

    @property
    def id(self) -> str:
        return self._id

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    # --- Cancellation ---

    def cancel(self) -> None:
        """Request cooperative cancellation."""
        self._cancelled = True
        if self._session_dir is not None:
            cancel_sentinel = self._session_dir / "cancel"
            cancel_sentinel.touch()

    def _is_cancelled(self) -> bool:
        """Check if cancellation has been requested."""
        if self._cancelled:
            return True
        if self.cancel_check is not None and self.cancel_check():
            self._cancelled = True
            return True
        return False

    # --- Async context manager (for Daytona cleanup) ---

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Clean up resources (Daytona sandbox teardown, etc.)."""
        pass  # local backend needs no cleanup

    # --- Main execution ---

    async def run(
        self,
        prompt: str,
        *,
        max_turns: int | None = None,
        on_event: EventHandler | None = None,
    ) -> RunResult:
        """Drive the agentic loop: LLM call -> tool dispatch -> repeat until done.

        Args:
            prompt: User prompt to send.
            max_turns: Max number of internal LLM round-trips. None = unlimited.
            on_event: Optional callback for streaming events.

        Returns:
            RunResult with text, events, status, and usage for this run.
        """
        self._state.run_count += 1
        self._state.status = "running"
        self._state._emit({"event": "run_start", "prompt": prompt})

        collected_text: list[str] = []
        collected_events: list[AgentEvent] = []
        run_usage: dict[str, int | float] = {}
        status = "completed"

        try:
            async for event in self._run_loop(prompt, max_turns=max_turns):
                collected_events.append(event)

                if event.type == "text" and event.content:
                    collected_text.append(event.content)
                elif event.type == "turn_complete" and event.usage:
                    run_usage = event.usage
                elif event.type == "cancelled":
                    status = "cancelled"
                elif event.type == "error":
                    status = "error"

                if on_event:
                    maybe_awaitable = on_event(event)
                    if maybe_awaitable is not None:
                        await maybe_awaitable
        except Exception:
            status = "error"
            raise
        finally:
            self._state.status = status
            self._state._emit({"event": "run_end", "status": status})

        return RunResult(
            text="".join(collected_text),
            events=collected_events,
            status=status,
            usage=run_usage,
        )

    async def _run_loop(
        self, user_input: str, *, max_turns: int | None = None
    ) -> AsyncIterator[AgentEvent]:
        """Internal agentic loop — yields AgentEvent."""
        self._cancelled = False
        self._nudge_count = 0
        self._call_index = 0

        # Auto-compact check before processing
        if self._should_auto_compact():
            async for ev in self._do_compact():
                yield ev

        # Add user message
        if user_input:
            self._state.add_user_message(user_input)

        turn = 0
        while max_turns is None or turn < max_turns:
            turn += 1

            # Check cancellation
            if self._is_cancelled():
                yield AgentEvent(type="cancelled", content="Cancelled before model call")
                return

            # Auto-compact check
            if self._should_auto_compact():
                async for ev in self._do_compact():
                    yield ev

            # Build prompt
            llm_prompt = Prompt(
                system=self._agent.system_prompt,
                messages=[
                    Message(
                        role=m["role"],
                        content=m.get("content"),
                        tool_calls=m.get("tool_calls"),
                        tool_call_id=m.get("tool_call_id"),
                    )
                    for m in self._state.get_messages()
                ],
                tools=self._registry.get_specs(),
            )

            # Track what we get
            text_content = ""
            tool_calls: list[dict[str, Any]] = []
            pending_tool_calls: list[tuple[str, str, dict[str, Any]]] = []

            # Stream response
            self._state._emit({
                "event": "llm_start",
                "model": self._agent.config.model,
                "context_size": self._state.estimate_tokens(self._agent.system_prompt),
            })

            async for event in self._client.stream(llm_prompt):
                if self._is_cancelled():
                    yield AgentEvent(type="cancelled", content="Cancelled during model response")
                    return

                if event.type == "text":
                    text_content += event.content or ""
                    yield AgentEvent(type="text", content=event.content)

                elif event.type == "tool_call":
                    tc = event.tool_call
                    if tc:
                        yield AgentEvent(
                            type="tool_start",
                            tool_name=tc.name,
                            tool_call_id=tc.id,
                            tool_args=tc.arguments,
                        )
                        tool_calls.append({
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        })
                        pending_tool_calls.append((tc.id, tc.name, tc.arguments))

                elif event.type == "done":
                    if event.usage:
                        self._state.update_usage(
                            input_tokens=event.usage.get("input_tokens", 0),
                            output_tokens=event.usage.get("output_tokens", 0),
                            cached_tokens=event.usage.get("cached_tokens", 0),
                            reasoning_tokens=event.usage.get("reasoning_tokens", 0),
                            cost_usd=event.usage.get("cost_usd", 0.0),
                        )
                        self._last_input_tokens = event.usage.get("input_tokens", 0)
                        self._call_index += 1

                        self._state._emit({
                            "event": "llm_end",
                            "model": self._agent.config.model,
                            "input_tokens": event.usage.get("input_tokens", 0),
                            "output_tokens": event.usage.get("output_tokens", 0),
                            "cache_read_tokens": event.usage.get("cached_tokens", 0),
                            "reasoning_tokens": event.usage.get("reasoning_tokens", 0),
                            "cost_usd": event.usage.get("cost_usd", 0.0),
                        })

                        yield AgentEvent(
                            type="api_call_complete",
                            usage={
                                "input_tokens": event.usage.get("input_tokens", 0),
                                "output_tokens": event.usage.get("output_tokens", 0),
                                "cached_tokens": event.usage.get("cached_tokens", 0),
                                "reasoning_tokens": event.usage.get("reasoning_tokens", 0),
                                "cost_usd": event.usage.get("cost_usd", 0.0),
                                "call_index": self._call_index,
                            },
                        )

                elif event.type == "error":
                    yield AgentEvent(type="error", content=event.content)
                    return

            # Record what the assistant said/did
            if tool_calls:
                self._state.add_assistant_tool_calls(tool_calls)
            elif text_content:
                self._state.add_assistant_message(text_content)

            # No tool calls — check nudge or finish
            if not pending_tool_calls:
                if self.enable_nudge and self._nudge_count < MAX_NUDGES:
                    text_lower = text_content.lower()
                    has_completion = any(s in text_lower for s in COMPLETION_SIGNALS)
                    if not has_completion and len(text_content) < 500:
                        self._nudge_count += 1
                        self._state.add_user_message(NUDGE_MESSAGE)
                        continue

                self._nudge_count = 0
                yield AgentEvent(
                    type="turn_complete",
                    usage={
                        "total_input_tokens": self._state.usage["input_tokens"],
                        "total_output_tokens": self._state.usage["output_tokens"],
                        "total_cached_tokens": self._state.usage["cached_tokens"],
                        "total_reasoning_tokens": self._state.usage["reasoning_tokens"],
                        "total_cost_usd": self._state.usage["cost_usd"],
                        "context_size": self._last_input_tokens,
                    },
                )
                return

            # Check cancellation before tool execution
            if self._is_cancelled():
                yield AgentEvent(type="cancelled", content="Cancelled before tool execution")
                return

            # Execute tool calls
            rejected = False
            for i, (tool_id, tool_name, tool_args) in enumerate(pending_tool_calls):
                if self._is_cancelled():
                    yield AgentEvent(type="cancelled", content="Cancelled before tool execution")
                    return

                # Check approval
                approved = True
                if self.approval_callback and self._registry.requires_approval(tool_name):
                    try:
                        approved = await self.approval_callback(tool_name, tool_args)
                    except ApprovalInterrupt:
                        # TODO: handle interruption for out-of-band approval
                        yield AgentEvent(
                            type="interruption",
                            tool_name=tool_name,
                            tool_call_id=tool_id,
                            tool_args=tool_args,
                        )
                        return

                if not approved:
                    self._state.add_tool_result(
                        tool_id,
                        "Command rejected by user. Awaiting new instructions.",
                    )
                    yield AgentEvent(
                        type="tool_blocked",
                        tool_name=tool_name,
                        tool_call_id=tool_id,
                        tool_args=tool_args,
                    )
                    self._state._emit({
                        "event": "tool_blocked",
                        "tool_call_id": tool_id,
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                    })
                    rejected = True
                    # Add dummy results for remaining
                    for remaining_id, _, _ in pending_tool_calls[i + 1:]:
                        self._state.add_tool_result(
                            remaining_id,
                            "Command skipped - user rejected previous command.",
                        )
                    break

                self._state._emit({
                    "event": "tool_start",
                    "tool_call_id": tool_id,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                })

                invocation = ToolInvocation(
                    call_id=tool_id,
                    tool_name=tool_name,
                    arguments=tool_args,
                )
                output = await self._registry.dispatch(invocation)
                truncated_content = truncate_output(output.content)

                self._state.add_tool_result(tool_id, truncated_content)
                self._state._emit({
                    "event": "tool_end",
                    "tool_call_id": tool_id,
                    "tool_name": tool_name,
                    "success": output.success,
                })

                yield AgentEvent(
                    type="tool_end",
                    tool_name=tool_name,
                    tool_call_id=tool_id,
                    tool_result=truncated_content,
                    tool_metadata=output.metadata,
                )

            if rejected:
                yield AgentEvent(
                    type="turn_complete",
                    usage={
                        "total_input_tokens": self._state.usage["input_tokens"],
                        "total_output_tokens": self._state.usage["output_tokens"],
                        "total_cached_tokens": self._state.usage["cached_tokens"],
                        "total_reasoning_tokens": self._state.usage["reasoning_tokens"],
                        "total_cost_usd": self._state.usage["cost_usd"],
                        "context_size": self._last_input_tokens,
                    },
                )
                return

    # --- Compaction ---

    def _should_auto_compact(self) -> bool:
        if not self.auto_compact or self.context_window is None:
            return False
        token_count = (
            self._last_input_tokens
            or self._state.estimate_tokens(self._agent.system_prompt)
        )
        threshold = int(self.context_window * AUTO_COMPACT_THRESHOLD)
        return token_count > threshold

    async def _do_compact(self, custom_instructions: str = "", trigger: str = "auto") -> AsyncIterator[AgentEvent]:
        yield AgentEvent(type="compact_start", content=trigger)
        result = await self.compact(custom_instructions=custom_instructions, trigger=trigger)
        yield AgentEvent(
            type="compact_end",
            content=f"Compacted: {result.tokens_before} -> {result.tokens_after} tokens",
        )

    async def compact(
        self, custom_instructions: str = "", trigger: str = "manual"
    ) -> CompactResult:
        """Compact conversation history via cache-safe forking."""
        tokens_before = self._state.estimate_tokens(self._agent.system_prompt)

        compaction_msg = COMPACTION_SYSTEM_PROMPT
        if custom_instructions:
            compaction_msg += f"\n\nUser guidance: {custom_instructions}"

        history_messages = [
            Message(
                role=m["role"],
                content=m.get("content"),
                tool_calls=m.get("tool_calls"),
                tool_call_id=m.get("tool_call_id"),
            )
            for m in self._state.get_messages()
        ]
        history_messages.append(Message(role="user", content=compaction_msg))

        prompt = Prompt(
            system=self._agent.system_prompt,
            messages=history_messages,
            tools=self._registry.get_specs(),
        )

        summary, usage = await self._client.complete_prompt(prompt)
        self._state.update_usage(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cached_tokens=usage.get("cached_tokens", 0),
            reasoning_tokens=usage.get("reasoning_tokens", 0),
            cost_usd=usage.get("cost_usd", 0.0),
        )

        formatted_summary = SUMMARY_PREFIX + summary
        user_messages = self._state.get_user_messages()
        recent_messages = user_messages[-3:] if len(user_messages) > 3 else []
        self._state.replace_with_summary(formatted_summary, recent_messages)

        tokens_after = self._state.estimate_tokens(self._agent.system_prompt)

        self._state._emit({
            "event": "compact",
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "trigger": trigger,
        })

        return CompactResult(
            summary=summary,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            trigger=trigger,
        )
