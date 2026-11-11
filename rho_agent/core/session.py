"""Session — execution context coordinating Agent + State through the agentic loop.

One session = one conversation thread. Creating a session is synchronous.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent import Agent

from ..client.model import Message, ModelCallError, Prompt
from ..tools.base import ToolInvocation
from ..tools.registry import ToolRegistry
from .events import (
    COMPACTION_SYSTEM_PROMPT,
    SUMMARY_PREFIX,
    AgentEvent,
    ApprovalCallback,
    ApprovalInterrupt,
    CompactResult,
    EventHandler,
    RunResult,
)
from .models import compact_threshold
from .state import State
from .truncate import truncate_output

# Recent history kept verbatim through a compaction, in estimated tokens.
COMPACT_TAIL_TOKENS = 20_000
# Smaller tail used when compacting to recover from a context overflow.
COMPACT_TAIL_TOKENS_AGGRESSIVE = 5_000
# Tool results longer than this are clipped in the summarisation request when
# recovering from an overflow (the stored history is not changed).
COMPACT_CLIP_CHARS = 2_000

INTERRUPTED_TOOL_RESULT = "Interrupted before this tool call finished."


class Session:
    """Execution context coordinating Agent + State through the agentic loop.

    LLM call -> tool dispatch -> LLM call -> ... until the model stops calling tools.

    One Session = one conversation thread. Each ``run()`` call appends to State,
    so the agent retains full context across runs. Supports:
    - **Multi-run conversations**: call ``run()`` multiple times for follow-ups.
    - **Streaming**: pass ``on_event`` to receive AgentEvents as they happen.
    - **Cancellation**: ``cancel()`` sets a cooperative flag (also writes a sentinel
      file when the session has a directory, for cross-process cancellation).
    - **Auto-compaction**: when context approaches the window limit, automatically
      summarizes history to stay within bounds.
    - **Async context manager**: ``async with Session(agent) as s:`` for backends
      that need cleanup (e.g. Daytona sandbox teardown).
    """

    def __init__(
        self,
        agent: Agent,
        *,
        session_id: str | None = None,
        state: State | None = None,
        session_dir: Path | None = None,
        client: Any | None = None,
    ) -> None:
        self._agent = agent
        self._id = session_id or str(uuid.uuid4())
        self._state = state or State()
        self._client = client or agent.create_client()
        self._cancelled = False
        self._session_dir = session_dir

        # Execution-time settings (set by callers before run())
        self.approval_callback: ApprovalCallback | None = None
        self.cancel_check: Callable[[], bool] | None = None
        self.auto_compact: bool = True
        config = agent.config
        # None means: look the model up in core/models.py.
        self.context_window: int | None = getattr(config, "context_window", None)
        self.compact_at_tokens: int | None = getattr(config, "compact_at_tokens", None)
        self.budget_gate: Callable[[int], str | None] | None = None

        # Hard caps for the whole session (all run() calls). None = no cap.
        self.max_cost_usd: float | None = getattr(config, "max_cost_usd", None)
        self.max_wall_seconds: float | None = getattr(config, "max_wall_seconds", None)

        # Session-level retry of transient model errors (on top of SDK retries).
        self.max_model_retries: int = 3
        self.retry_backoff_seconds: float = 2.0

        # Shared reference to agent's registry (treat as frozen during execution)
        self._registry = agent.registry

        # Internal run state
        self._last_input_tokens: int = 0
        self._call_index: int = 0
        self._wall_start: float | None = None
        # After a failed compaction, do not try again until context grows past this.
        self._compact_retry_above: int = 0

    @property
    def agent(self) -> Agent:
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
        """Check if cancellation has been requested (including sentinel file)."""
        if self._cancelled:
            return True
        # Check sentinel file in session directory
        if self._session_dir is not None and (self._session_dir / "cancel").exists():
            self._cancelled = True
            return True
        if self.cancel_check is not None and self.cancel_check():
            self._cancelled = True
            return True
        return False

    async def _check_pause(self) -> None:
        """Sleep-poll while the pause sentinel exists. Returns when unpaused or cancelled."""
        if self._session_dir is None:
            return
        pause_path = self._session_dir / "pause"
        if not pause_path.exists():
            return
        while pause_path.exists():
            if self._is_cancelled():
                return
            await asyncio.sleep(0.5)

    def _consume_directives(self) -> list[str]:
        """Read and truncate directives.jsonl with fcntl locking. Returns list of directive strings."""
        if self._session_dir is None:
            return []
        path = self._session_dir / "directives.jsonl"
        if not path.exists():
            return []
        directives: list[str] = []
        try:
            with open(path, "r+", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                obj = json.loads(line)
                                directives.append(obj.get("text", line))
                            except json.JSONDecodeError:
                                directives.append(line)
                    f.seek(0)
                    f.truncate()
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
        except FileNotFoundError:
            pass
        return directives

    def _update_meta_status(self, status: str) -> None:
        """Write final status to meta.json if session directory exists."""
        if self._session_dir is None:
            return
        meta_path = self._session_dir / "meta.json"
        if not meta_path.exists():
            return
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["status"] = status
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except Exception:
            pass  # best-effort

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

    async def get_sandbox(self) -> Any:
        """Return the Daytona sandbox, creating it if needed.

        Use this to upload/download files before or after ``run()``::

            async with Session(agent) as s:
                sandbox = await s.get_sandbox()
                await sandbox.fs.upload_file("local/data.csv", "/work/data.csv")
                await s.run(prompt="Analyze /work/data.csv")
                await sandbox.fs.download_file("/work/results.json", "results.json")

        Raises:
            RuntimeError: If the agent is not using a Daytona backend.
        """
        manager = self._agent.sandbox_manager
        if manager is None:
            raise RuntimeError("get_sandbox() requires a Daytona backend")
        return await manager.get_sandbox()

    async def close(self) -> None:
        """Clean up resources (Daytona sandbox teardown, etc.)."""
        manager = self._agent.sandbox_manager
        if manager is not None:
            await manager.close()

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
            RunResult with text, events, status, and usage for this run. Status
            is one of ``completed``, ``error``, ``cancelled``, ``max_turns`` or
            ``budget_exceeded``.
        """
        if self._wall_start is None:
            self._wall_start = time.monotonic()
        # A previous run that was killed or interrupted may have left tool calls
        # without results; the API rejects such a history.
        self._state.close_pending_tool_calls(INTERRUPTED_TOOL_RESULT)

        self._state.run_count += 1
        self._state.status = "running"
        self._state._emit({"type": "run_start", "prompt": prompt})

        collected_text: list[str] = []
        collected_events: list[AgentEvent] = []
        run_usage: dict[str, int | float] = {}
        status = "completed"
        # Index into collected_text where the current model call's text begins,
        # so text from a failed attempt can be dropped when the call is retried.
        call_text_mark = 0

        try:
            async for event in self._run_loop(prompt, max_turns=max_turns):
                collected_events.append(event)

                if event.type == "text" and event.content:
                    collected_text.append(event.content)
                elif event.type == "api_call_complete":
                    call_text_mark = len(collected_text)
                elif event.type == "retry":
                    del collected_text[call_text_mark:]
                elif event.type == "turn_complete" and event.usage:
                    run_usage = event.usage
                elif event.type == "cancelled":
                    status = "cancelled"
                elif event.type == "error":
                    status = "error"
                elif event.type in ("max_turns", "budget_exceeded"):
                    status = event.type

                if on_event:
                    maybe_awaitable = on_event(event)
                    if maybe_awaitable is not None:
                        await maybe_awaitable
        except Exception:
            status = "error"
            raise
        finally:
            # Whatever ended the run, every tool call must have a result.
            self._state.close_pending_tool_calls(INTERRUPTED_TOOL_RESULT)
            self._state.status = status
            self._state._emit({"type": "run_end", "status": status})
            self._update_meta_status(status)

        return RunResult(
            text="".join(collected_text),
            events=collected_events,
            status=status,
            usage=run_usage,
        )

    # --- Budget ---

    def _wall_remaining(self) -> float | None:
        """Seconds left under the wall-clock cap, or None when there is no cap."""
        if self.max_wall_seconds is None or self._wall_start is None:
            return None
        return self.max_wall_seconds - (time.monotonic() - self._wall_start)

    def _budget_exceeded(self) -> str | None:
        """Return a reason if a hard cap has been reached, else None."""
        if self.max_cost_usd is not None:
            spent = float(self._state.usage.get("cost_usd", 0.0) or 0.0)
            if spent >= self.max_cost_usd:
                return f"Cost cap reached: ${spent:.2f} spent, cap ${self.max_cost_usd:.2f}"
        remaining = self._wall_remaining()
        if remaining is not None and remaining <= 0:
            return f"Time cap reached: {self.max_wall_seconds:.0f}s"
        return None

    def _turn_complete_event(self) -> AgentEvent:
        return AgentEvent(
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

    @staticmethod
    def _to_messages(messages: list[dict[str, Any]]) -> list[Message]:
        return [
            Message(
                role=m["role"],
                content=m.get("content"),
                tool_calls=m.get("tool_calls"),
                tool_call_id=m.get("tool_call_id"),
            )
            for m in messages
        ]

    async def _run_loop(
        self, user_input: str, *, max_turns: int | None = None
    ) -> AsyncIterator[AgentEvent]:
        """Internal agentic loop — yields AgentEvent."""
        self._cancelled = False
        self._call_index = 0

        # Add user message
        if user_input:
            self._state.add_user_message(user_input)

        turn = 0
        overflow_recoveries = 0
        while True:
            if max_turns is not None and turn >= max_turns:
                yield self._turn_complete_event()
                yield AgentEvent(type="max_turns", content=f"Stopped after {max_turns} model turns")
                return
            turn += 1

            # Check pause sentinel
            await self._check_pause()

            # Consume directives between turns
            for directive in self._consume_directives():
                self._state.add_user_message(directive)

            # Check cancellation
            if self._is_cancelled():
                yield AgentEvent(type="cancelled", content="Cancelled before model call")
                return

            # Hard caps
            exceeded = self._budget_exceeded()
            if exceeded:
                yield self._turn_complete_event()
                yield AgentEvent(type="budget_exceeded", content=exceeded)
                return

            # Auto-compact check
            if self._should_auto_compact():
                async for ev in self._do_compact():
                    yield ev

            llm_prompt = Prompt(
                system=self._agent.system_prompt,
                messages=self._to_messages(self._state.get_messages()),
                tools=self._registry.get_specs(),
            )

            # Model call, retried on transient errors.
            attempt = 0
            recompact = False
            while True:
                text_content = ""
                tool_calls: list[dict[str, Any]] = []
                pending_tool_calls: list[tuple[str, str, dict[str, Any], str | None]] = []
                error_event: Any | None = None

                self._state._emit(
                    {
                        "type": "llm_start",
                        "model": self._agent.config.model,
                        "context_size": self._state.estimate_tokens(self._agent.system_prompt),
                    }
                )

                async for event in self._client.stream(llm_prompt):
                    if self._is_cancelled():
                        yield AgentEvent(
                            type="cancelled", content="Cancelled during model response"
                        )
                        return

                    remaining = self._wall_remaining()
                    if remaining is not None and remaining <= 0:
                        yield self._turn_complete_event()
                        yield AgentEvent(type="budget_exceeded", content=self._budget_exceeded())
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
                            tool_calls.append(
                                {
                                    "id": tc.id,
                                    "type": "function",
                                    "function": {
                                        "name": tc.name,
                                        "arguments": json.dumps(tc.arguments),
                                    },
                                }
                            )
                            pending_tool_calls.append(
                                (tc.id, tc.name, tc.arguments, getattr(tc, "parse_error", None))
                            )

                    elif event.type == "done":
                        if event.usage:
                            usage = {
                                "input_tokens": event.usage.get("input_tokens", 0),
                                "output_tokens": event.usage.get("output_tokens", 0),
                                "cached_tokens": event.usage.get("cached_tokens", 0),
                                "reasoning_tokens": event.usage.get("reasoning_tokens", 0),
                                "cost_usd": event.usage.get("cost_usd", 0.0),
                            }
                            self._state.update_usage(**usage)
                            self._last_input_tokens = usage["input_tokens"]
                            self._call_index += 1

                            self._state._emit(
                                {
                                    "type": "llm_end",
                                    "model": self._agent.config.model,
                                    "input_tokens": usage["input_tokens"],
                                    "output_tokens": usage["output_tokens"],
                                    "cache_read_tokens": usage["cached_tokens"],
                                    "reasoning_tokens": usage["reasoning_tokens"],
                                    "cost_usd": usage["cost_usd"],
                                }
                            )

                            yield AgentEvent(
                                type="api_call_complete",
                                usage={**usage, "call_index": self._call_index},
                            )

                    elif event.type == "error":
                        error_event = event
                        break

                if error_event is None:
                    break

                # Clients that do not classify their errors get no retry.
                kind = getattr(error_event, "error_kind", None) or "fatal"
                self._state._emit({"type": "llm_error", "kind": kind, "error": error_event.content})

                if kind == "context_overflow" and overflow_recoveries < 2:
                    overflow_recoveries += 1
                    yield AgentEvent(
                        type="retry",
                        content="Context window exceeded; compacting history and retrying",
                    )
                    async for ev in self._do_compact(trigger="overflow", aggressive=True):
                        yield ev
                    recompact = True
                    break

                if kind == "transient" and attempt < self.max_model_retries:
                    attempt += 1
                    yield AgentEvent(
                        type="retry",
                        content=(
                            f"Model call failed ({error_event.content}); "
                            f"retry {attempt}/{self.max_model_retries}"
                        ),
                    )
                    await asyncio.sleep(self.retry_backoff_seconds * 2 ** (attempt - 1))
                    continue

                yield AgentEvent(type="error", content=error_event.content)
                return

            if recompact:
                turn -= 1  # the failed call does not count against max_turns
                continue

            # Record what the assistant said/did
            if tool_calls:
                self._state.add_assistant_tool_calls(tool_calls, content=text_content or None)
            elif text_content:
                self._state.add_assistant_message(text_content)

            # No tool calls — turn is done
            if not pending_tool_calls:
                yield self._turn_complete_event()
                return

            # Execute tool calls. If the loop exits early for any reason, run()
            # gives the remaining calls an "interrupted" result.
            rejected = False
            for i, (tool_id, tool_name, tool_args, parse_error) in enumerate(pending_tool_calls):
                if self._is_cancelled():
                    yield AgentEvent(type="cancelled", content="Cancelled before tool execution")
                    return

                if parse_error:
                    content = (
                        f"Error: the {tool_name} call was not executed because its "
                        f"{parse_error}\nResend the call with valid JSON arguments."
                    )
                    self._state.add_tool_result(tool_id, content)
                    self._state._emit(
                        {
                            "type": "tool_end",
                            "tool_call_id": tool_id,
                            "tool_name": tool_name,
                            "success": False,
                        }
                    )
                    yield AgentEvent(
                        type="tool_end",
                        tool_name=tool_name,
                        tool_call_id=tool_id,
                        tool_result=content,
                        tool_metadata={"parse_error": True},
                    )
                    continue

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
                    self._state._emit(
                        {
                            "type": "tool_blocked",
                            "tool_call_id": tool_id,
                            "tool_name": tool_name,
                            "tool_args": tool_args,
                        }
                    )
                    rejected = True
                    # Add dummy results for remaining
                    for remaining_id, _, _, _ in pending_tool_calls[i + 1 :]:
                        self._state.add_tool_result(
                            remaining_id,
                            "Command skipped - user rejected previous command.",
                        )
                    break

                self._state._emit(
                    {
                        "type": "tool_start",
                        "tool_call_id": tool_id,
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                    }
                )

                invocation = ToolInvocation(
                    call_id=tool_id,
                    tool_name=tool_name,
                    arguments=tool_args,
                )
                remaining = self._wall_remaining()
                timed_out = False
                try:
                    if remaining is None:
                        output = await self._registry.dispatch(invocation)
                    elif remaining <= 0:
                        timed_out = True
                    else:
                        output = await asyncio.wait_for(
                            self._registry.dispatch(invocation), timeout=remaining
                        )
                except TimeoutError:
                    timed_out = True

                if timed_out:
                    content = "Interrupted: the session time cap was reached."
                    self._state.add_tool_result(tool_id, content)
                    self._state._emit(
                        {
                            "type": "tool_end",
                            "tool_call_id": tool_id,
                            "tool_name": tool_name,
                            "success": False,
                        }
                    )
                    yield AgentEvent(
                        type="tool_end",
                        tool_name=tool_name,
                        tool_call_id=tool_id,
                        tool_result=content,
                        tool_metadata={"timed_out": True},
                    )
                    yield self._turn_complete_event()
                    yield AgentEvent(type="budget_exceeded", content=self._budget_exceeded())
                    return

                truncated_content = truncate_output(output.content)

                self._state.add_tool_result(tool_id, truncated_content)
                self._state._emit(
                    {
                        "type": "tool_end",
                        "tool_call_id": tool_id,
                        "tool_name": tool_name,
                        "success": output.success,
                    }
                )

                yield AgentEvent(
                    type="tool_end",
                    tool_name=tool_name,
                    tool_call_id=tool_id,
                    tool_result=truncated_content,
                    tool_metadata=output.metadata,
                )

            if rejected:
                yield self._turn_complete_event()
                return

            # Budget gate — inject a wrap-up message if context is getting full
            if self.budget_gate is not None:
                gate_msg = self.budget_gate(self._last_input_tokens)
                if gate_msg is not None:
                    self._state.add_user_message(gate_msg)

    # --- Compaction ---

    def _compact_threshold(self) -> int:
        return compact_threshold(
            self._agent.config.model,
            context_window=self.context_window,
            compact_at_tokens=self.compact_at_tokens,
        )

    def _should_auto_compact(self) -> bool:
        if not self.auto_compact:
            return False
        token_count = self._last_input_tokens or self._state.estimate_tokens(
            self._agent.system_prompt
        )
        if token_count <= self._compact_retry_above:
            return False
        return token_count > self._compact_threshold()

    async def _do_compact(
        self, custom_instructions: str = "", trigger: str = "auto", aggressive: bool = False
    ) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(type="compact_start", content=trigger)
        result = await self.compact(
            custom_instructions=custom_instructions, trigger=trigger, aggressive=aggressive
        )
        if result.error:
            content = f"Compaction failed, history unchanged: {result.error}"
        else:
            content = f"Compacted: {result.tokens_before} -> {result.tokens_after} tokens"
        yield AgentEvent(type="compact_end", content=content)

    @staticmethod
    def _message_tokens(message: dict[str, Any]) -> int:
        chars = len(str(message.get("content") or ""))
        if message.get("tool_calls"):
            chars += len(str(message["tool_calls"]))
        return chars // 4

    def _split_for_compaction(
        self, messages: list[dict[str, Any]], tail_tokens: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split history into (head to summarise, recent tail kept verbatim).

        The tail never starts on a tool result, so a tool call and its results
        always stay on the same side of the split.
        """
        start = len(messages)
        used = 0
        while start > 0:
            cost = self._message_tokens(messages[start - 1])
            if used + cost > tail_tokens:
                break
            used += cost
            start -= 1
        while start < len(messages) and messages[start].get("role") == "tool":
            start += 1
        return messages[:start], messages[start:]

    async def compact(
        self,
        custom_instructions: str = "",
        trigger: str = "manual",
        aggressive: bool = False,
    ) -> CompactResult:
        """Summarise older history, keeping the original task and a recent tail.

        After compaction the history is: the first user message (the task), a
        summary of everything up to the tail, then the tail verbatim. If the
        summarisation call fails the history is left unchanged.

        Args:
            custom_instructions: Extra guidance for the summary.
            trigger: ``manual``, ``auto`` or ``overflow`` (recorded in the trace).
            aggressive: Keep a smaller tail and clip long tool results in the
                summarisation request. Used to recover from a context overflow,
                where the full history no longer fits in one request.
        """
        system_prompt = self._agent.system_prompt
        tokens_before = self._state.estimate_tokens(system_prompt)
        messages = self._state.get_messages()

        tail_budget = COMPACT_TAIL_TOKENS_AGGRESSIVE if aggressive else COMPACT_TAIL_TOKENS
        tail_budget = min(tail_budget, max(self._compact_threshold() // 4, 1))
        head, tail = self._split_for_compaction(messages, tail_budget)
        if len(head) < 2:
            # Nothing worth summarising (history is already just the tail).
            return CompactResult(
                summary="", tokens_before=tokens_before, tokens_after=tokens_before, trigger=trigger
            )

        compaction_msg = COMPACTION_SYSTEM_PROMPT
        if custom_instructions:
            compaction_msg += f"\n\nUser guidance: {custom_instructions}"

        to_summarise = head
        if aggressive:
            to_summarise = []
            for m in head:
                content = m.get("content")
                if (
                    m.get("role") == "tool"
                    and isinstance(content, str)
                    and len(content) > COMPACT_CLIP_CHARS
                ):
                    half = COMPACT_CLIP_CHARS // 2
                    m = {**m, "content": f"{content[:half]}\n...[clipped]...\n{content[-half:]}"}
                to_summarise.append(m)

        history_messages = self._to_messages(to_summarise)
        history_messages.append(Message(role="user", content=compaction_msg))
        prompt = Prompt(
            system=system_prompt,
            messages=history_messages,
            tools=self._registry.get_specs(),
        )

        try:
            summary, usage = await self._client.complete_prompt(prompt)
        except ModelCallError as e:
            # Do not retry on every turn: wait until the context has grown.
            current = self._last_input_tokens or tokens_before
            self._compact_retry_above = int(current * 1.1)
            self._state._emit({"type": "compact_failed", "trigger": trigger, "error": str(e)})
            return CompactResult(
                summary="",
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                trigger=trigger,
                error=str(e),
            )

        usage_totals = {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cached_tokens": usage.get("cached_tokens", 0),
            "reasoning_tokens": usage.get("reasoning_tokens", 0),
            "cost_usd": usage.get("cost_usd", 0.0),
        }
        self._state.update_usage(**usage_totals)
        # Recorded as llm_end so usage replays correctly on resume.
        self._state._emit(
            {
                "type": "llm_end",
                "model": self._agent.config.model,
                "purpose": "compaction",
                "input_tokens": usage_totals["input_tokens"],
                "output_tokens": usage_totals["output_tokens"],
                "cache_read_tokens": usage_totals["cached_tokens"],
                "reasoning_tokens": usage_totals["reasoning_tokens"],
                "cost_usd": usage_totals["cost_usd"],
            }
        )

        new_messages: list[dict[str, Any]] = []
        first_user = next((m for m in head if m.get("role") == "user"), None)
        if first_user is not None:
            new_messages.append(first_user)
        new_messages.append({"role": "user", "content": SUMMARY_PREFIX + summary})
        new_messages.extend(tail)
        self._state.replace_messages(new_messages)

        tokens_after = self._state.estimate_tokens(system_prompt)
        # The last real input count describes the old history; use the estimate
        # until the next model call reports a new one.
        self._last_input_tokens = tokens_after
        self._compact_retry_above = 0

        self._state._emit(
            {
                "type": "compact",
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "trigger": trigger,
            }
        )

        return CompactResult(
            summary=summary,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            trigger=trigger,
        )
