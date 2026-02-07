"""Shared runtime types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..core.agent import Agent, AgentEvent
from ..core.session import Session
from ..observability.processor import ObservabilityProcessor
from ..tools.registry import ToolRegistry

ApprovalCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]
EventHandler = Callable[[AgentEvent], None | Awaitable[None]]


@dataclass
class AgentRuntime:
    """Runtime bundle for execution."""

    agent: Agent
    session: Session
    registry: ToolRegistry
    model: str
    profile_name: str
    session_id: str
    observability: ObservabilityProcessor | None = None


@dataclass
class RunResult:
    """Final results for one run_turn call."""

    text: str
    events: list[AgentEvent]
    status: str
    usage: dict[str, int]
