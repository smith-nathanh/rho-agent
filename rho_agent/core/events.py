"""Shared event types, constants, and callbacks for the core agent API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# Auto-compaction triggers at this fraction of the model's context window.
AUTO_COMPACT_THRESHOLD = 0.7

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

DEFAULT_GATE_PROMPT = """\
Before finishing, verify your work against the original request:
1. Re-read the request and check each requirement against what you actually produced.
2. If there are tests or checks you can run, run them and read their output. \
An exit code of 0 alone does not mean they passed.
3. Confirm that every file or output the request asks for exists where it should.

If everything is verified, reply with exactly {token}. Otherwise fix the problems first."""


@dataclass
class CompletionGate:
    """A check the model must pass before a run is allowed to end.

    When the model stops calling tools, the session sends ``prompt`` instead of
    ending the run. The run ends once the model replies with exactly ``token``,
    or after ``max_rounds`` prompts. ``{token}`` in the prompt is substituted.
    """

    prompt: str = DEFAULT_GATE_PROMPT
    token: str = "TASK_VERIFIED"
    max_rounds: int = 3

    def render(self) -> str:
        return self.prompt.replace("{token}", self.token)


# Type for approval callback: (tool_name, tool_args) -> approved
ApprovalCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]

# Type for compaction callback: (trigger: "manual" | "auto") -> None
CompactCallback = Callable[[str], Awaitable[None]]

# Type for event handler
EventHandler = Callable[["AgentEvent"], None | Awaitable[None]]


@dataclass
class AgentEvent:
    """Event emitted during a Session.run() execution.

    Events stream to the ``on_event`` callback as the agent works, enabling
    live UIs (streaming text, tool progress, usage tracking). The ``type``
    field determines which other fields are populated.

    Event types:
    - ``text``: Streaming text chunk from the model (``content`` set).
    - ``tool_start``: Tool invocation started (``tool_name``, ``tool_call_id``, ``tool_args``).
    - ``tool_end``: Tool completed (``tool_name``, ``tool_result``, ``tool_metadata``).
    - ``tool_blocked``: Tool call rejected by user approval (``tool_name``, ``tool_args``).
    - ``api_call_complete``: Single LLM API call finished (``usage`` with per-call metrics).
    - ``turn_complete``: Full agentic turn finished (``usage`` with cumulative session metrics).
    - ``compact_start``/``compact_end``: Context window compaction lifecycle.
    - ``error``: An error occurred (``content`` has the message).
    - ``retry``: A model call failed and is being retried (``content`` says why).
      Text streamed by the failed attempt should be discarded.
    - ``completion_gate``: The model tried to finish and was asked to verify first
      (``content`` is the prompt that was sent).
    - ``max_turns``: The run stopped at its ``max_turns`` limit.
    - ``budget_exceeded``: A cost or time cap ended the run (``content`` says which).
    - ``cancelled``: Run was cancelled cooperatively.
    - ``interruption``: Run paused for out-of-band tool approval.
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
    trigger: str  # "manual", "auto" or "overflow"
    error: str | None = None  # set when the summarisation call failed


@dataclass
class RunResult:
    """Result of a single ``session.run()`` call.

    Contains the final concatenated text response, all events from the run,
    the terminal status, and token usage/cost for this run only (not cumulative
    session totals — those live on ``session.state.usage``).
    """

    text: str
    events: list[AgentEvent]
    status: str  # "completed", "error", "cancelled", "max_turns", "budget_exceeded"
    usage: dict[str, int | float]


class ApprovalInterrupt(Exception):
    """Raised by approval callbacks to pause execution for external approval."""
