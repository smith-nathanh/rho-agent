"""rho-agent: An agent harness with first-class CLI and programmatic APIs."""

from __future__ import annotations

__version__ = "0.1.0"

from .core import (
    Agent,
    AgentConfig,
    AgentEvent,
    RunResult,
    Session,
    SessionStore,
    State,
)

__all__ = [
    "__version__",
    "Agent",
    "AgentConfig",
    "AgentEvent",
    "RunResult",
    "Session",
    "SessionStore",
    "State",
]
