"""Local SQLite-based event stream via polling."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from ..storage.protocol import TelemetryStore
from .protocol import TelemetryEvent

POLL_INTERVAL_S = 1.0


class LocalEventStream:
    """EventStream implementation that polls SQLite for changes.

    This wraps existing storage queries into the EventStream interface,
    yielding TelemetryEvent objects as new turns and tool executions appear.
    """

    def __init__(self, storage: TelemetryStore) -> None:
        self._storage = storage

    async def subscribe(self, session_id: str) -> AsyncIterator[TelemetryEvent]:
        """Poll storage for new events on the given session."""
        last_turn_index = -1
        seen_execution_ids: set[str] = set()

        while True:
            detail = await asyncio.to_thread(
                self._storage.get_session_detail, session_id
            )
            if detail is None:
                await asyncio.sleep(POLL_INTERVAL_S)
                continue

            for turn in detail.turns:
                turn_index = turn.get("turn_index", -1)
                if turn_index > last_turn_index:
                    last_turn_index = turn_index
                    yield TelemetryEvent(
                        event_type="turn_start",
                        table="turns",
                        row_id=turn.get("turn_id", ""),
                        timestamp=datetime.now(timezone.utc),
                        data=turn,
                    )

                for te in turn.get("tool_executions", []):
                    eid = te.get("execution_id", "")
                    if eid and eid not in seen_execution_ids:
                        seen_execution_ids.add(eid)
                        yield TelemetryEvent(
                            event_type="tool_execution",
                            table="tool_executions",
                            row_id=eid,
                            timestamp=datetime.now(timezone.utc),
                            data=te,
                        )

            if detail.status != "active":
                yield TelemetryEvent(
                    event_type="session_end",
                    table="sessions",
                    row_id=session_id,
                    timestamp=datetime.now(timezone.utc),
                    data={"status": detail.status},
                )
                return

            await asyncio.sleep(POLL_INTERVAL_S)
