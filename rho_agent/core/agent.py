"""Core agent loop for rho-agent."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Awaitable
from dataclasses import dataclass
from typing import Any

from ..client.model import ModelClient, Prompt, Message
from ..tools.base import ToolInvocation
from ..tools.registry import ToolRegistry
from .session import Session, ToolResult
from .truncate import truncate_output

# Auto-compaction triggers at this fraction of the model's context window
AUTO_COMPACT_THRESHOLD = 0.7

# Continuation nudge settings (eval mode only)
MAX_NUDGES = 3
NUDGE_MESSAGE = (
    "Please continue working on the task. If you need a tool that's missing, "
    "install it. If an approach failed, try a different method."
)
COMPLETION_SIGNALS = [
    "task complete",
    "successfully completed",
    "finished",
    "done",
    "completed the task",
    "solution is ready",
    "have completed",
    "is complete",
]

COMPACTION_SYSTEM_PROMPT = """\
You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences discovered
- What remains to be done (clear next steps)
- Any critical data, file paths, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work."""

SUMMARY_PREFIX = """\
Another language model worked on this task and produced a summary of its progress. Use this to build on the work that has already been done and avoid duplicating effort. Here is the summary:

"""

# Type for approval callback: (tool_name, tool_args) -> approved
ApprovalCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]

# Type for compaction callback: (trigger: "manual" | "auto") -> None
CompactCallback = Callable[[str], Awaitable[None]]


@dataclass
class AgentEvent:
    """Event emitted by the agent during execution.

    Event types:
    - "text": Streaming text content from the model
    - "tool_start": Tool invocation started
    - "tool_end": Tool invocation completed
    - "api_call_complete": Single API call finished (per-call metrics)
    - "turn_complete": Full turn finished (cumulative metrics)
    - "error": An error occurred
    - "tool_blocked": Tool call was blocked by user
    - "interruption": Run paused waiting for out-of-band tool approval
    - "compact_start": Context compaction starting
    - "compact_end": Context compaction finished
    - "cancelled": Turn was cancelled
    """

    type: str
    content: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: str | None = None
    tool_metadata: dict[str, Any] | None = None  # Metadata from tool output
    usage: dict[str, int] | None = None


@dataclass
class CompactResult:
    """Result of a compaction operation."""

    summary: str
    tokens_before: int
    tokens_after: int
    trigger: str  # "manual" or "auto"


class ApprovalInterrupt(Exception):
    """Raised by approval callbacks to pause execution for external approval."""


class Agent:
    """The core agent that orchestrates the conversation loop.

    Follows the pattern:
    1. Build prompt with history and tools
    2. Stream response from model
    3. Execute any tool calls
    4. If tools were called, loop back with results
    5. When model produces final text, turn is complete
    """

    def __init__(
        self,
        session: Session,
        registry: ToolRegistry,
        client: ModelClient | None = None,
        approval_callback: ApprovalCallback | None = None,
        context_window: int | None = None,
        auto_compact: bool = True,
        cancel_check: Callable[[], bool] | None = None,
        enable_nudge: bool = False,
    ) -> None:
        self._session = session
        self._registry = registry
        self._client = client or ModelClient()
        self._approval_callback = approval_callback
        self._context_window = context_window
        self._auto_compact = auto_compact
        self._cancel_requested = False
        self._cancel_check = cancel_check
        self._enable_nudge = enable_nudge
        self._nudge_count = 0
        self._call_index = 0  # Tracks API calls within current turn
        self._interrupted_tool_calls: list[tuple[str, str, dict[str, Any]]] = []

    def request_cancel(self) -> None:
        """Request cancellation of the current turn."""
        self._cancel_requested = True

    def set_registry(self, registry: ToolRegistry) -> None:
        """Swap the active tool registry for subsequent model calls."""
        self._registry = registry

    def set_approval_callback(self, approval_callback: ApprovalCallback | None) -> None:
        """Swap the active approval callback used for tool-gated calls."""
        self._approval_callback = approval_callback

    def _reset_cancel(self) -> None:
        """Reset cancellation state for a new turn."""
        self._cancel_requested = False
        self._nudge_count = 0
        self._call_index = 0
        self._interrupted_tool_calls.clear()

    def consume_interrupted_tool_calls(self) -> list[tuple[str, str, dict[str, Any]]]:
        """Return and clear tool calls queued during an approval interruption."""
        pending = [(call_id, name, dict(args)) for call_id, name, args in self._interrupted_tool_calls]
        self._interrupted_tool_calls.clear()
        return pending

    def is_cancelled(self) -> bool:
        """Check if cancellation has been requested.

        Checks both the in-process flag (set by request_cancel) and the
        optional external cancel_check callback (e.g., file-based signal).
        """
        if self._cancel_requested:
            return True
        if self._cancel_check is not None and self._cancel_check():
            self._cancel_requested = True  # latch so subsequent checks are fast
            return True
        return False

    async def compact(
        self, custom_instructions: str = "", trigger: str = "manual"
    ) -> CompactResult:
        """Compact the conversation history into a summary.

        Args:
            custom_instructions: Optional guidance for what to prioritize in summary.
            trigger: "manual" (user-initiated) or "auto" (context limit reached).

        Returns:
            CompactResult with summary and token counts.
        """
        tokens_before = self._session.estimate_tokens()

        # Build the summarization prompt
        system = COMPACTION_SYSTEM_PROMPT
        if custom_instructions:
            system += f"\n\nUser guidance: {custom_instructions}"

        # Build conversation content for summarization
        conversation_text = self._format_history_for_summary()

        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": f"Here is the conversation to summarize:\n\n{conversation_text}",
            },
        ]

        # Get the summary from the model
        summary, usage = await self._client.complete(messages)
        self._session.update_token_usage(
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            usage.get("cached_tokens", 0),
            usage.get("reasoning_tokens", 0),
            usage.get("cost_usd", 0.0),
        )

        # Format the summary with prefix
        formatted_summary = SUMMARY_PREFIX + summary

        # Get recent user messages to preserve (last 2-3 for context)
        user_messages = self._session.get_user_messages()
        recent_messages = user_messages[-3:] if len(user_messages) > 3 else []

        # Replace history with summary
        self._session.replace_with_summary(formatted_summary, recent_messages)

        tokens_after = self._session.estimate_tokens()

        return CompactResult(
            summary=summary,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            trigger=trigger,
        )

    def _format_history_for_summary(self) -> str:
        """Format conversation history as text for summarization."""
        parts = []
        for msg in self._session.get_messages():
            role = msg.get("role", "unknown")
            content = msg.get("content")

            if role == "user":
                parts.append(f"User: {content}")
            elif role == "assistant":
                if content:
                    parts.append(f"Assistant: {content}")
                if msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        func = tc.get("function", {})
                        parts.append(f"Assistant called tool: {func.get('name', 'unknown')}")
            elif role == "tool":
                # Summarize tool results briefly
                result = content or ""
                if len(result) > 500:
                    result = result[:500] + "..."
                parts.append(f"Tool result: {result}")

        return "\n\n".join(parts)

    def should_auto_compact(self) -> bool:
        """Check if auto-compaction should be triggered."""
        if not self._auto_compact or self._context_window is None:
            return False
        # Prefer actual token count from last API call over heuristic estimate
        token_count = self._session.last_input_tokens or self._session.estimate_tokens()
        threshold = int(self._context_window * AUTO_COMPACT_THRESHOLD)
        return token_count > threshold

    async def run_turn(
        self,
        user_input: str,
        *,
        pending_tool_calls: list[tuple[str, str, dict[str, Any]]] | None = None,
        approval_overrides: dict[str, bool] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run a single conversation turn.

        This may involve multiple model calls if tools are invoked.
        Yields AgentEvent(type="cancelled") if cancellation is requested.
        """
        self._reset_cancel()

        # Check if auto-compaction is needed before processing
        if self.should_auto_compact():
            yield AgentEvent(type="compact_start", content="auto")
            result = await self.compact(trigger="auto")
            yield AgentEvent(
                type="compact_end",
                content=f"Compacted: {result.tokens_before} → {result.tokens_after} tokens",
            )

        # Add user message to history for a fresh run. Resumed runs continue
        # from existing assistant tool calls without inserting a blank message.
        if user_input:
            self._session.add_user_message(user_input)

        queued_tool_calls = list(pending_tool_calls or [])

        # Loop until we get a final response (no more tool calls)
        while True:
            pending_tool_calls_for_execution = queued_tool_calls
            queued_tool_calls = []

            if pending_tool_calls_for_execution:
                # Skip model call and resume by executing already-emitted tool calls.
                text_content = ""
            else:
                # Check for cancellation before model call
                if self.is_cancelled():
                    yield AgentEvent(type="cancelled", content="Cancelled before model call")
                    return

                # Check if auto-compaction is needed before next model call
                if self.should_auto_compact():
                    yield AgentEvent(type="compact_start", content="auto")
                    result = await self.compact(trigger="auto")
                    yield AgentEvent(
                        type="compact_end",
                        content=f"Compacted: {result.tokens_before} → {result.tokens_after} tokens",
                    )

                # Build prompt
                prompt = Prompt(
                    system=self._session.system_prompt,
                    messages=[
                        Message(
                            role=m["role"],
                            content=m.get("content"),
                            tool_calls=m.get("tool_calls"),
                            tool_call_id=m.get("tool_call_id"),
                        )
                        for m in self._session.get_messages()
                    ],
                    tools=self._registry.get_specs(),
                )

                # Track what we get in this turn
                text_content = ""
                tool_calls: list[dict[str, Any]] = []
                pending_tool_calls_for_execution = []  # (id, name, args)

                # Stream response
                async for event in self._client.stream(prompt):
                    # Check for cancellation during streaming
                    if self.is_cancelled():
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
                            # OpenAI format for tool calls
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
                            pending_tool_calls_for_execution.append((tc.id, tc.name, tc.arguments))

                    elif event.type == "done":
                        if event.usage:
                            self._session.update_token_usage(
                                event.usage.get("input_tokens", 0),
                                event.usage.get("output_tokens", 0),
                                event.usage.get("cached_tokens", 0),
                                event.usage.get("reasoning_tokens", 0),
                                event.usage.get("cost_usd", 0.0),
                            )

                            # Emit per-call metrics
                            self._call_index += 1
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
                    self._session.add_assistant_tool_calls(tool_calls)
                elif text_content:
                    self._session.add_assistant_message(text_content)

                # If no tool calls, check if we should nudge or finish
                if not pending_tool_calls_for_execution:
                    # Check if we should nudge (eval mode only)
                    if self._enable_nudge and self._nudge_count < MAX_NUDGES:
                        text_lower = text_content.lower()
                        has_completion = any(s in text_lower for s in COMPLETION_SIGNALS)
                        # Nudge if no completion signal and response is short
                        if not has_completion and len(text_content) < 500:
                            self._nudge_count += 1
                            self._session.add_user_message(NUDGE_MESSAGE)
                            continue  # Loop back to model

                    # Reset nudge count and finish turn
                    self._nudge_count = 0
                    yield AgentEvent(
                        type="turn_complete",
                        usage={
                            "total_input_tokens": self._session.total_input_tokens,
                            "total_output_tokens": self._session.total_output_tokens,
                            "total_cached_tokens": self._session.total_cached_tokens,
                            "total_reasoning_tokens": self._session.total_reasoning_tokens,
                            "total_cost_usd": self._session.total_cost_usd,
                            "context_size": self._session.last_input_tokens,
                        },
                    )
                    return

            # Check for cancellation before model call
            if self.is_cancelled():
                yield AgentEvent(type="cancelled", content="Cancelled before tool execution")
                return

            # Execute tool calls
            tool_results: list[ToolResult] = []
            rejected = False
            for i, (tool_id, tool_name, tool_args) in enumerate(pending_tool_calls_for_execution):
                # Check for cancellation before each tool
                if self.is_cancelled():
                    yield AgentEvent(type="cancelled", content="Cancelled before tool execution")
                    return

                # Check approval if callback is set and tool requires it
                approved_override = None
                if approval_overrides and tool_id in approval_overrides:
                    approved_override = approval_overrides[tool_id]

                approval_checked = False
                approved = True
                if approved_override is not None:
                    approval_checked = True
                    approved = approved_override
                elif self._approval_callback and self._registry.requires_approval(tool_name):
                    approval_checked = True
                    try:
                        approved = await self._approval_callback(tool_name, tool_args)
                    except ApprovalInterrupt:
                        if tool_results:
                            self._session.add_tool_results(tool_results)
                        self._interrupted_tool_calls = [
                            (call_id, name, dict(args))
                            for call_id, name, args in pending_tool_calls_for_execution[i:]
                        ]
                        yield AgentEvent(
                            type="interruption",
                            tool_name=tool_name,
                            tool_call_id=tool_id,
                            tool_args=tool_args,
                        )
                        return

                if approval_checked and not approved:
                    # Must add result to keep API happy, then end turn
                    tool_results.append(
                        ToolResult(
                            tool_call_id=tool_id,
                            content="Command rejected by user. Awaiting new instructions.",
                        )
                    )
                    yield AgentEvent(
                        type="tool_blocked",
                        tool_name=tool_name,
                        tool_call_id=tool_id,
                        tool_args=tool_args,
                    )
                    rejected = True
                    # Add dummy results for remaining tool calls
                    for remaining_id, _, _ in pending_tool_calls_for_execution[i + 1 :]:
                        tool_results.append(
                            ToolResult(
                                tool_call_id=remaining_id,
                                content="Command skipped - user rejected previous command.",
                            )
                        )
                    break

                invocation = ToolInvocation(
                    call_id=tool_id,
                    tool_name=tool_name,
                    arguments=tool_args,
                )
                output = await self._registry.dispatch(invocation)
                # Truncate output to prevent context overflow
                truncated_content = truncate_output(output.content)
                tool_results.append(
                    ToolResult(
                        tool_call_id=tool_id,
                        content=truncated_content,
                    )
                )
                yield AgentEvent(
                    type="tool_end",
                    tool_name=tool_name,
                    tool_call_id=tool_id,
                    tool_result=truncated_content,
                    tool_metadata=output.metadata,
                )

            # Add tool results to history
            self._session.add_tool_results(tool_results)

            # If user rejected, end turn now (don't loop back to model)
            if rejected:
                yield AgentEvent(
                    type="turn_complete",
                    usage={
                        "total_input_tokens": self._session.total_input_tokens,
                        "total_output_tokens": self._session.total_output_tokens,
                        "total_cached_tokens": self._session.total_cached_tokens,
                        "total_reasoning_tokens": self._session.total_reasoning_tokens,
                        "total_cost_usd": self._session.total_cost_usd,
                        "context_size": self._session.last_input_tokens,
                    },
                )
                return
