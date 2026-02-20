"""Postgres-backed transport for cross-node agent control."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from rho_agent.control.models import AgentStatus, RunningAgent
from rho_agent.observability.heartbeat import STALE_THRESHOLD_S

logger = logging.getLogger(__name__)


class PostgresSignalTransport:
    """Control transport backed by Postgres agent_registry and signal_queue.

    Satisfies the ``ControlTransport`` protocol for multi-node deployments.
    """

    def __init__(self, dsn: str) -> None:
        import psycopg_pool

        self._dsn = dsn
        self._pool = psycopg_pool.ConnectionPool(dsn, min_size=1, max_size=4, open=True)

    def list_running(self) -> list[RunningAgent]:
        """List agents with a recent heartbeat (not stale)."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                SELECT session_id, pid, model, instruction_preview,
                       started_at, heartbeat_at, status, labels
                FROM agent_registry
                WHERE heartbeat_at > NOW() - INTERVAL '%s seconds'
                ORDER BY started_at DESC
                """,
                (STALE_THRESHOLD_S,),
            )
            cols = [d[0] for d in cur.description] if cur.description else []
            agents: list[RunningAgent] = []
            for row in cur.fetchall():
                r = dict(zip(cols, row))
                labels_raw = r.get("labels")
                if isinstance(labels_raw, str):
                    labels_raw = json.loads(labels_raw)
                agents.append(
                    RunningAgent(
                        session_id=r["session_id"],
                        pid=r["pid"],
                        model=r["model"],
                        instruction_preview=r.get("instruction_preview") or "",
                        started_at=r.get("started_at"),
                        status=AgentStatus(r.get("status", "running")),
                        labels=labels_raw or {},
                        heartbeat_at=r.get("heartbeat_at"),
                    )
                )
            return agents

    def register_launcher_session(
        self,
        session_id: str,
        *,
        pid: int,
        model: str,
        instruction_preview: str,
        labels: dict[str, str] | None = None,
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO agent_registry (session_id, pid, model, instruction_preview, labels)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE SET
                    pid = EXCLUDED.pid, heartbeat_at = NOW(), status = 'running'
                """,
                (session_id, pid, model, instruction_preview, json.dumps(labels or {})),
            )
            conn.commit()

    def deregister(self, session_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM agent_registry WHERE session_id = %s", (session_id,))
            conn.commit()

    def kill(self, session_id: str) -> bool:
        return self._enqueue_signal(session_id, "cancel")

    def pause(self, session_id: str) -> bool:
        return self._enqueue_signal(session_id, "pause")

    def resume(self, session_id: str) -> bool:
        return self._enqueue_signal(session_id, "resume")

    def directive(self, session_id: str, text: str) -> bool:
        return self._enqueue_signal(session_id, "directive", payload=text)

    def _enqueue_signal(
        self, session_id: str, signal_type: str, payload: str | None = None
    ) -> bool:
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO signal_queue (session_id, signal_type, payload)
                    VALUES (%s, %s, %s)
                    """,
                    (session_id, signal_type, payload),
                )
                conn.commit()
            return True
        except Exception:
            logger.warning("Failed to enqueue %s for %s", signal_type, session_id[:8])
            return False

    def close(self) -> None:
        self._pool.close()


@dataclass
class PendingSignal:
    """A signal consumed from the signal_queue."""

    id: int
    signal_type: str
    payload: str | None


class PostgresSignalReceiver:
    """Agent-side consumer of signals from the Postgres signal_queue.

    Call ``consume_pending()`` periodically from the agent's cancel-check.
    """

    def __init__(self, dsn: str, session_id: str) -> None:
        self._dsn = dsn
        self._session_id = session_id
        self._conn: Any = None

    def _ensure_conn(self) -> Any:
        if self._conn is None or getattr(self._conn, "closed", True):
            import psycopg

            self._conn = psycopg.connect(self._dsn, autocommit=True)
        return self._conn

    def consume_pending(self) -> list[PendingSignal]:
        """Consume and return all unconsumed signals for this session."""
        try:
            conn = self._ensure_conn()
            cur = conn.execute(
                """
                UPDATE signal_queue
                SET consumed_at = NOW()
                WHERE session_id = %s AND consumed_at IS NULL
                RETURNING id, signal_type, payload
                """,
                (self._session_id,),
            )
            cols = [d[0] for d in cur.description] if cur.description else []
            return [
                PendingSignal(
                    id=r["id"],
                    signal_type=r["signal_type"],
                    payload=r.get("payload"),
                )
                for row in cur.fetchall()
                for r in [dict(zip(cols, row))]
            ]
        except Exception:
            logger.debug("Signal consume failed for %s", self._session_id[:8])
            self._conn = None
            return []

    def is_cancelled(self) -> bool:
        """Check if a cancel signal has been received."""
        signals = self.consume_pending()
        return any(s.signal_type == "cancel" for s in signals)

    def close(self) -> None:
        if self._conn and not getattr(self._conn, "closed", True):
            self._conn.close()
            self._conn = None
