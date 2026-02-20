"""Postgres LISTEN/NOTIFY-based event stream."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from .protocol import TelemetryEvent

logger = logging.getLogger(__name__)


class PostgresEventStream:
    """EventStream implementation using Postgres LISTEN/NOTIFY.

    Subscribes to the ``rho_agent_events`` channel and filters events
    for the requested session_id, hydrating row data from the DB.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    async def subscribe(self, session_id: str) -> AsyncIterator[TelemetryEvent]:
        """Listen for NOTIFY events and yield matching TelemetryEvents."""
        import psycopg

        async with await psycopg.AsyncConnection.connect(
            self._dsn, autocommit=True
        ) as conn:
            await conn.execute("LISTEN rho_agent_events")

            async for notify in conn.notifies():
                try:
                    payload = json.loads(notify.payload) if notify.payload else {}
                except (json.JSONDecodeError, TypeError):
                    continue

                table = payload.get("table", "")
                row_id = payload.get("id", "")
                op = payload.get("op", "")

                # Filter: only events related to our session
                if not await self._belongs_to_session(conn, table, row_id, session_id):
                    continue

                event_type = self._map_event_type(table, op)
                data = await self._hydrate(conn, table, row_id)

                yield TelemetryEvent(
                    event_type=event_type,
                    table=table,
                    row_id=row_id,
                    timestamp=datetime.now(timezone.utc),
                    data=data,
                )

                # Stop on session end
                if table == "sessions" and data.get("status") not in ("active", None):
                    return

    @staticmethod
    async def _belongs_to_session(
        conn: Any, table: str, row_id: str, session_id: str
    ) -> bool:
        """Check if the event's row belongs to the given session."""
        if table == "sessions":
            return row_id == session_id

        if table == "turns":
            cur = await conn.execute(
                "SELECT session_id FROM turns WHERE turn_id = %s", (row_id,)
            )
            row = await cur.fetchone()
            return row is not None and row[0] == session_id

        if table == "tool_executions":
            cur = await conn.execute(
                """
                SELECT t.session_id FROM tool_executions te
                JOIN turns t ON te.turn_id = t.turn_id
                WHERE te.execution_id = %s
                """,
                (row_id,),
            )
            row = await cur.fetchone()
            return row is not None and row[0] == session_id

        return False

    @staticmethod
    def _map_event_type(table: str, op: str) -> str:
        mapping = {
            ("sessions", "INSERT"): "session_start",
            ("sessions", "UPDATE"): "session_update",
            ("turns", "INSERT"): "turn_start",
            ("turns", "UPDATE"): "turn_end",
            ("tool_executions", "INSERT"): "tool_execution",
        }
        return mapping.get((table, op), f"{table}_{op.lower()}")

    @staticmethod
    async def _hydrate(conn: Any, table: str, row_id: str) -> dict[str, Any]:
        """Fetch the full row data for the event."""
        id_col = {
            "sessions": "session_id",
            "turns": "turn_id",
            "tool_executions": "execution_id",
        }.get(table)
        if not id_col:
            return {}

        cur = await conn.execute(
            f"SELECT * FROM {table} WHERE {id_col} = %s",  # noqa: S608
            (row_id,),
        )
        row = await cur.fetchone()
        if not row or not cur.description:
            return {}
        cols = [d[0] for d in cur.description]
        result: dict[str, Any] = {}
        for col, val in zip(cols, row):
            if isinstance(val, datetime):
                result[col] = val.isoformat()
            else:
                result[col] = val
        return result
