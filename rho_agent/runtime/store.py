"""Persistent storage backends for interrupted run state."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Protocol

from .types import RunState


class RunStore(Protocol):
    """Storage interface for serializable run state."""

    def save(self, run_id: str, state: RunState) -> None:
        """Persist run state for a logical run id."""

    def load(self, run_id: str) -> RunState | None:
        """Load a previously-saved run state by run id."""

    def delete(self, run_id: str) -> None:
        """Delete persisted run state by run id."""


class SqliteRunStore:
    """SQLite-backed ``RunStore`` implementation."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        """Open a WAL-mode connection to the backing database."""
        conn = sqlite3.connect(str(self.path))
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_states (
                    run_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def save(self, run_id: str, state: RunState) -> None:
        """Persist run state for a logical run id."""
        payload = json.dumps(state.to_dict(), ensure_ascii=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_states(run_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id)
                DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at
                """,
                (run_id, payload, time.time()),
            )

    def load(self, run_id: str) -> RunState | None:
        """Load a previously-saved run state by run id."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM run_states WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        return RunState.from_dict(data)

    def delete(self, run_id: str) -> None:
        """Delete persisted run state by run id."""
        with self._connect() as conn:
            conn.execute("DELETE FROM run_states WHERE run_id = ?", (run_id,))
