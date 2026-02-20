"""Telemetry context for tracking sessions and spans."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import ObservabilityConfig


def _generate_id() -> str:
    """Generate a unique ID for sessions/spans."""
    return str(uuid.uuid4())


def _utc_now() -> datetime:
    """Get current UTC time."""
    return datetime.now(timezone.utc)


@dataclass
class TelemetryContext:
    """Context for a telemetry session.

    Tracks the hierarchical structure:
    - Session (root): One per agent invocation
    - Turn: One per user input/response cycle
    - Span: Individual operations (model calls, tool executions)
    """

    # Tenant identification (required for multi-tenancy)
    team_id: str
    project_id: str

    # Session identification
    session_id: str = field(default_factory=_generate_id)
    agent_id: str = ""  # Instance identifier (optional)

    # Environment metadata
    environment: str = "development"  # production, staging, development
    profile: str = "readonly"  # Capability profile name
    model: str = ""  # Model being used

    # Session state
    started_at: datetime = field(default_factory=_utc_now)
    ended_at: datetime | None = None
    status: str = "active"  # active, completed, error

    # Aggregated metrics
    total_turns: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_reasoning_tokens: int = 0
    total_tool_calls: int = 0
    total_cost_usd: float = 0.0
    context_size: int = 0  # Current conversation context size (last API call's input tokens)

    # Current turn tracking
    current_turn_id: str | None = None
    current_turn_index: int = 0

    # Additional metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(
        cls,
        config: ObservabilityConfig,
        model: str = "",
        profile: str = "readonly",
        environment: str | None = None,
        agent_id: str = "",
    ) -> TelemetryContext:
        """Create context from observability config.

        Args:
            config: Observability configuration with tenant info.
            model: Model being used.
            profile: Capability profile name.
            environment: Environment name (defaults to 'development').
            agent_id: Optional agent instance identifier.

        Returns:
            New TelemetryContext instance.

        Raises:
            ValueError: If config has no tenant information.
        """
        if not config.tenant:
            raise ValueError("ObservabilityConfig must have tenant information")

        import os

        env = environment or os.getenv("RHO_AGENT_ENVIRONMENT", "development")

        metadata: dict[str, Any] = {}
        if config.labels:
            metadata["labels"] = dict(config.labels)

        return cls(
            team_id=config.tenant.team_id,
            project_id=config.tenant.project_id,
            model=model,
            profile=profile,
            environment=env,
            agent_id=agent_id,
            metadata=metadata,
        )

    def start_turn(self) -> str:
        """Start a new turn and return its ID."""
        self.current_turn_index += 1
        self.current_turn_id = _generate_id()
        self.total_turns += 1
        return self.current_turn_id

    def end_turn(self) -> None:
        """End the current turn."""
        self.current_turn_id = None

    def record_tokens(
        self,
        input_tokens: int,
        output_tokens: int,
        reasoning_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Record token usage and cost."""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_reasoning_tokens += reasoning_tokens
        self.total_cost_usd += cost_usd

    def record_tool_call(self) -> None:
        """Record a tool call."""
        self.total_tool_calls += 1

    def end_session(self, status: str = "completed") -> None:
        """Mark session as ended."""
        self.ended_at = _utc_now()
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "team_id": self.team_id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "environment": self.environment,
            "profile": self.profile,
            "model": self.model,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "status": self.status,
            "total_turns": self.total_turns,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_reasoning_tokens": self.total_reasoning_tokens,
            "total_tool_calls": self.total_tool_calls,
            "total_cost_usd": self.total_cost_usd,
            "context_size": self.context_size,
            "metadata": self.metadata,
        }


@dataclass
class TurnContext:
    """Context for a single turn within a session."""

    turn_id: str
    session_id: str
    turn_index: int
    started_at: datetime = field(default_factory=_utc_now)
    ended_at: datetime | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    user_input: str = ""
    context_size: int = 0  # Context size at end of turn

    def end(self) -> None:
        """Mark turn as ended."""
        self.ended_at = _utc_now()

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "turn_index": self.turn_index,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": self.cost_usd,
            "tool_calls": self.tool_calls,
            "context_size": self.context_size,
        }


@dataclass
class ToolExecutionContext:
    """Context for a single tool execution."""

    execution_id: str = field(default_factory=_generate_id)
    turn_id: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result: str | None = None
    success: bool = True
    error: str | None = None
    started_at: datetime = field(default_factory=_utc_now)
    ended_at: datetime | None = None
    duration_ms: int = 0

    def end(self, success: bool = True, error: str | None = None) -> None:
        """Mark execution as ended."""
        self.ended_at = _utc_now()
        self.success = success
        self.error = error
        if self.started_at and self.ended_at:
            delta = self.ended_at - self.started_at
            self.duration_ms = int(delta.total_seconds() * 1000)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "execution_id": self.execution_id,
            "turn_id": self.turn_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "result": self.result,
            "success": self.success,
            "error": self.error,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_ms": self.duration_ms,
        }
