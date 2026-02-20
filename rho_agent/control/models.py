"""Shared models for command center control and live feeds."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class AgentStatus(StrEnum):
    """Current control-plane status for a running agent."""

    RUNNING = "running"
    PAUSED = "paused"


@dataclass(slots=True)
class RunningAgent:
    """Agent visible in command-center running roster."""

    session_id: str
    pid: int
    model: str
    instruction_preview: str
    started_at: datetime | None
    status: AgentStatus


@dataclass(slots=True)
class LaunchRequest:
    """Request to launch a new agent process."""

    working_dir: Path
    profile: str = "readonly"
    model: str = "gpt-5-mini"
    prompt: str = ""
    auto_approve: bool = False


@dataclass(slots=True)
class LaunchedAgent:
    """Result of launching an agent."""

    session_id: str
    pid: int
    command: list[str]
    started_at: datetime


@dataclass(slots=True)
class ManagedProcess:
    """Process metadata tracked by launcher."""

    session_id: str
    pid: int
    command: list[str]
    working_dir: Path


@dataclass(slots=True)
class FeedCursor:
    """Cursor for incremental telemetry polling."""

    last_turn_index: int = -1
    last_tool_started_at: str = ""


@dataclass(slots=True)
class TrajectoryEvent:
    """Single event in live trajectory stream."""

    event_type: str
    timestamp: datetime | None
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class FeedDelta:
    """Incremental updates from telemetry feed poll."""

    events: list[TrajectoryEvent] = field(default_factory=list)
    cursor: FeedCursor = field(default_factory=FeedCursor)
