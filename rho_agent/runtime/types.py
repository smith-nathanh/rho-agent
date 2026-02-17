"""Shared runtime types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from ..core.agent import Agent, AgentEvent
from ..core.session import Session
from ..observability.processor import ObservabilityProcessor
from ..tools.registry import ToolRegistry
from .options import RuntimeOptions

ApprovalCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]
EventHandler = Callable[[AgentEvent], None | Awaitable[None]]


@dataclass
class ToolApprovalItem:
    """Tool call paused for out-of-band approval."""

    tool_call_id: str
    tool_name: str
    tool_args: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolApprovalItem:
        return cls(
            tool_call_id=str(data["tool_call_id"]),
            tool_name=str(data["tool_name"]),
            tool_args=dict(data.get("tool_args", {})),
        )


@dataclass
class RunState:
    """Serializable state envelope for interrupted runs."""

    session_id: str
    system_prompt: str
    history: list[dict[str, Any]]
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_tokens: int = 0
    total_reasoning_tokens: int = 0
    total_cost_usd: float = 0.0
    last_input_tokens: int = 0
    pending_approvals: list[ToolApprovalItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "system_prompt": self.system_prompt,
            "history": self.history,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cached_tokens": self.total_cached_tokens,
            "total_reasoning_tokens": self.total_reasoning_tokens,
            "total_cost_usd": self.total_cost_usd,
            "last_input_tokens": self.last_input_tokens,
            "pending_approvals": [item.to_dict() for item in self.pending_approvals],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        return cls(
            session_id=str(data["session_id"]),
            system_prompt=str(data["system_prompt"]),
            history=[dict(message) for message in data.get("history", [])],
            total_input_tokens=int(data.get("total_input_tokens", 0)),
            total_output_tokens=int(data.get("total_output_tokens", 0)),
            total_cached_tokens=int(data.get("total_cached_tokens", 0)),
            total_reasoning_tokens=int(data.get("total_reasoning_tokens", 0)),
            total_cost_usd=float(data.get("total_cost_usd", 0.0)),
            last_input_tokens=int(data.get("last_input_tokens", 0)),
            pending_approvals=[
                ToolApprovalItem.from_dict(item) for item in data.get("pending_approvals", [])
            ],
        )


@dataclass
class LocalRuntime:
    """Concrete local runtime — satisfies the ``Runtime`` protocol."""

    agent: Agent
    session: Session
    registry: ToolRegistry
    model: str
    profile_name: str
    session_id: str
    options: RuntimeOptions
    approval_callback: ApprovalCallback | None = None
    cancel_check: Callable[[], bool] | None = None
    observability: ObservabilityProcessor | None = None

    async def start(self) -> None:
        """Start runtime-level telemetry session if configured."""
        if self.observability:
            await self.observability.start_session()

    async def close(self, status: str = "completed") -> None:
        """Close runtime-level telemetry session if configured."""
        if self.observability:
            await self.observability.end_session(status)

    def restore_state(self, state: RunState) -> None:
        """Mutate runtime in-place from a serialized run snapshot."""
        self.session_id = state.session_id
        self.options.session_id = state.session_id
        if self.observability:
            self.observability.context.session_id = state.session_id
        self.session.system_prompt = state.system_prompt
        self.session.history = deepcopy(state.history)
        self.session.total_input_tokens = state.total_input_tokens
        self.session.total_output_tokens = state.total_output_tokens
        self.session.total_cached_tokens = state.total_cached_tokens
        self.session.total_reasoning_tokens = state.total_reasoning_tokens
        self.session.total_cost_usd = state.total_cost_usd
        self.session.last_input_tokens = state.last_input_tokens

    def capture_state(self, interruptions: list[ToolApprovalItem]) -> RunState:
        """Build a serializable run snapshot from the current runtime session."""
        return RunState(
            session_id=self.session_id,
            system_prompt=self.session.system_prompt,
            history=deepcopy(self.session.history),
            total_input_tokens=self.session.total_input_tokens,
            total_output_tokens=self.session.total_output_tokens,
            total_cached_tokens=self.session.total_cached_tokens,
            total_reasoning_tokens=self.session.total_reasoning_tokens,
            total_cost_usd=self.session.total_cost_usd,
            last_input_tokens=self.session.last_input_tokens,
            pending_approvals=interruptions,
        )


@dataclass
class RunResult:
    """Final results for one run_turn call."""

    text: str
    events: list[AgentEvent]
    status: str
    usage: dict[str, int | float]
    interruptions: list[ToolApprovalItem] = field(default_factory=list)
    state: RunState | None = None
