from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from rho_agent.observability.storage.sqlite import SessionDetail, SessionSummary


@dataclass(frozen=True)
class FeedCursor:
    """Cursor for incremental polling.

    Semantics: cursor is the count of turns already observed.
    """

    turn_count: int = 0


@dataclass(frozen=True)
class FeedEvent:
    """A single telemetry event emitted by the feed."""

    type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class FeedDelta:
    """Delta returned from a poll: new events + updated cursor."""

    events: list[FeedEvent]
    cursor: FeedCursor


class TelemetryStorageFacade(Protocol):
    def list_sessions(self, *, status: str | None = None, limit: int = 50) -> list[SessionSummary]: ...

    def get_session_detail(self, session_id: str) -> SessionDetail | None: ...

    def count_sessions(self, status: str | None = None) -> int: ...


class TelemetryFeed:
    """Service-layer abstraction over TelemetryStorage.

    Provides typed accessors plus cursor/delta polling for incremental updates.
    """

    def __init__(self, storage: TelemetryStorageFacade):
        self._storage = storage

    def list_recent_sessions(self, limit: int = 50, status: str | None = None) -> list[SessionSummary]:
        return self._storage.list_sessions(status=status, limit=limit)

    def get_session_detail(self, session_id: str) -> SessionDetail | None:
        return self._storage.get_session_detail(session_id)

    def poll_session_updates(self, session_id: str, cursor: FeedCursor | None) -> FeedDelta:
        cursor = cursor or FeedCursor(turn_count=0)
        detail = self._storage.get_session_detail(session_id)
        if not detail:
            return FeedDelta(events=[], cursor=cursor)

        turns: list[dict[str, Any]] = list(detail.turns or [])
        start = max(0, min(cursor.turn_count, len(turns)))
        new_turns = turns[start:]

        events: list[FeedEvent] = []
        for turn in new_turns:
            events.append(FeedEvent(type="turn", payload=turn))
            for tool in turn.get("tool_executions", []) or []:
                events.append(FeedEvent(type="tool_execution", payload=tool))

        new_cursor = FeedCursor(turn_count=len(turns))
        return FeedDelta(events=events, cursor=new_cursor)
