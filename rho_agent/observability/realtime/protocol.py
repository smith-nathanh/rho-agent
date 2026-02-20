"""EventStream protocol for real-time telemetry subscriptions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class TelemetryEvent:
    """Single event from a real-time telemetry stream."""

    event_type: str  # session_update, turn_start, turn_end, tool_execution
    table: str  # sessions, turns, tool_executions
    row_id: str
    timestamp: datetime | None = None
    data: dict[str, Any] = field(default_factory=dict)


class EventStream(Protocol):
    """Protocol for subscribing to real-time telemetry events."""

    async def subscribe(self, session_id: str) -> AsyncIterator[TelemetryEvent]:
        """Yield events for the given session as they arrive."""
        ...
