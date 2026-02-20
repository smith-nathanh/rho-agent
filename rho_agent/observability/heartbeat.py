"""Heartbeat sender for Postgres-backed agent registry."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 15
STALE_THRESHOLD_S = 45  # 3 missed heartbeats


class HeartbeatSender:
    """Async task that periodically updates ``heartbeat_at`` in agent_registry.

    Usage::

        hb = HeartbeatSender(dsn, session_id)
        task = asyncio.create_task(hb.run())
        # ... later ...
        task.cancel()
    """

    def __init__(self, dsn: str, session_id: str) -> None:
        self._dsn = dsn
        self._session_id = session_id
        self._conn: Any = None  # psycopg.AsyncConnection

    async def _ensure_conn(self) -> Any:
        if self._conn is None or self._conn.closed:
            import psycopg

            self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        return self._conn

    async def run(self) -> None:
        """Run the heartbeat loop until cancelled."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                try:
                    conn = await self._ensure_conn()
                    await conn.execute(
                        "UPDATE agent_registry SET heartbeat_at = NOW() WHERE session_id = %s",
                        (self._session_id,),
                    )
                except Exception:
                    logger.debug("Heartbeat update failed for %s", self._session_id[:8])
                    self._conn = None
        except asyncio.CancelledError:
            pass
        finally:
            if self._conn and not self._conn.closed:
                await self._conn.close()
