"""TelemetryStore protocol and shared data models for storage backends."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from ..context import TelemetryContext, ToolExecutionContext, TurnContext


# ── Shared data models ──────────────────────────────────────────────


@dataclass
class SessionSummary:
    """Summary of a session for listing."""

    session_id: str
    team_id: str
    project_id: str
    model: str
    started_at: datetime
    ended_at: datetime | None
    status: str
    total_input_tokens: int
    total_output_tokens: int
    total_reasoning_tokens: int
    total_tool_calls: int
    context_size: int
    turn_count: int


@dataclass
class SessionDetail:
    """Detailed session information including turns and tool executions."""

    session_id: str
    team_id: str
    project_id: str
    agent_id: str | None
    environment: str | None
    profile: str | None
    model: str
    started_at: datetime
    ended_at: datetime | None
    status: str
    total_input_tokens: int
    total_output_tokens: int
    total_reasoning_tokens: int
    total_tool_calls: int
    context_size: int
    metadata: dict[str, Any]
    turns: list[dict[str, Any]]


@dataclass
class ToolStats:
    """Statistics for tool usage."""

    tool_name: str
    total_calls: int
    success_count: int
    failure_count: int
    avg_duration_ms: float
    total_duration_ms: int


@dataclass
class CostSummary:
    """Cost/token summary for a time period."""

    team_id: str
    project_id: str
    total_sessions: int
    total_input_tokens: int
    total_output_tokens: int
    total_reasoning_tokens: int
    total_tool_calls: int


# ── Storage protocol ────────────────────────────────────────────────


class TelemetryStore(Protocol):
    """Backend-agnostic interface for telemetry storage.

    Both SQLite and Postgres backends satisfy this protocol.
    """

    def create_session(self, context: TelemetryContext) -> None: ...

    def update_session(self, context: TelemetryContext) -> None: ...

    def end_session(
        self,
        session_id: str,
        status: str = "completed",
        input_tokens: int = 0,
        output_tokens: int = 0,
        reasoning_tokens: int = 0,
        tool_calls: int = 0,
    ) -> None: ...

    def increment_session_tool_calls(self, session_id: str, count: int = 1) -> None: ...

    def create_turn(self, turn: TurnContext, user_input: str = "") -> None: ...

    def end_turn(self, turn: TurnContext) -> None: ...

    def record_tool_execution(self, execution: ToolExecutionContext) -> None: ...

    def list_sessions(
        self,
        team_id: str | None = None,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionSummary]: ...

    def count_sessions(self, status: str | None = None) -> int: ...

    def get_session_detail(self, session_id: str) -> SessionDetail | None: ...

    def get_tool_stats(
        self,
        team_id: str | None = None,
        project_id: str | None = None,
        days: int = 30,
    ) -> list[ToolStats]: ...

    def get_cost_summary(
        self,
        team_id: str | None = None,
        project_id: str | None = None,
        days: int = 30,
    ) -> list[CostSummary]: ...

    def get_active_sessions(self) -> list[SessionSummary]: ...
