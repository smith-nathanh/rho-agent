"""Shared event types, constants, and callbacks for the core agent API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# Auto-compaction triggers at this fraction of the model's context window.
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

# Type for event handler
EventHandler = Callable[["AgentEvent"], None | Awaitable[None]]


@dataclass
class AgentEvent:
    """Event emitted during execution.

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
    tool_metadata: dict[str, Any] | None = None
    usage: dict[str, int] | None = None


@dataclass
class CompactResult:
    """Result of a compaction operation."""

    summary: str
    tokens_before: int
    tokens_after: int
    trigger: str  # "manual" or "auto"


@dataclass
class RunResult:
    """Returned by session.run()."""

    text: str
    events: list[AgentEvent]
    status: str  # "completed", "error", "cancelled"
    usage: dict[str, int | float]


class ApprovalInterrupt(Exception):
    """Raised by approval callbacks to pause execution for external approval."""
