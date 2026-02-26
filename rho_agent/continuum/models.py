"""Data models for the continuum module."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any

from ..core.config import AgentConfig

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")


@dataclass
class VerificationConfig:
    """Commands for automated checks."""

    test_cmd: str | None = None
    lint_cmd: str | None = None
    typecheck_cmd: str | None = None


@dataclass
class SessionUsage:
    """Token usage for a single session."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class ContinuumConfig:
    """Configuration for a continuum run."""

    prd_path: str
    working_dir: str = "."
    model: str = field(default_factory=lambda: DEFAULT_MODEL)
    service_tier: str | None = field(default_factory=lambda: os.getenv("RHO_AGENT_SERVICE_TIER"))
    context_window: int = 400_000
    budget_threshold: float = 0.7
    max_sessions: int = 10
    test_cmd: str | None = None
    lint_cmd: str | None = None
    typecheck_cmd: str | None = None
    git_branch: str | None = None
    resume: bool = False
    state_path: str | None = None
    project_id: str | None = None
    team_id: str | None = None

    @property
    def verification(self) -> VerificationConfig:
        return VerificationConfig(
            test_cmd=self.test_cmd,
            lint_cmd=self.lint_cmd,
            typecheck_cmd=self.typecheck_cmd,
        )

    def agent_config(self, system_prompt: str) -> AgentConfig:
        """Build an AgentConfig for a continuum session."""
        return AgentConfig(
            system_prompt=system_prompt,
            model=self.model,
            service_tier=self.service_tier,
            profile="developer",
            working_dir=self.working_dir,
            auto_approve=True,
        )


@dataclass
class ContinuumState:
    """JSON-serializable persistence envelope."""

    run_id: str
    config: ContinuumConfig
    session_count: int = 0
    total_usage: SessionUsage = field(default_factory=SessionUsage)
    status: str = "running"
    last_handoff_number: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContinuumState:
        config = ContinuumConfig(**data["config"])
        total_usage = SessionUsage(**data.get("total_usage", {}))
        return cls(
            run_id=data["run_id"],
            config=config,
            session_count=data.get("session_count", 0),
            total_usage=total_usage,
            status=data.get("status", "running"),
            last_handoff_number=data.get("last_handoff_number", 0),
        )
