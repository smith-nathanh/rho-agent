from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator

import pytest

from rho_agent.core.agent import AgentEvent
from rho_agent.observability.config import ObservabilityConfig, TenantConfig
from rho_agent.observability.context import TelemetryContext
from rho_agent.observability.exporters.sqlite import SQLiteExporter
from rho_agent.observability.processor import ObservabilityProcessor
from rho_agent.observability.storage.sqlite import TelemetryStorage


class LockingStorage:
    def __init__(self) -> None:
        self.calls = 0

    def create_session(self, context: TelemetryContext) -> None:
        self.calls += 1
        if self.calls <= 2:
            raise sqlite3.OperationalError("database is locked")

    def update_session(self, context: TelemetryContext) -> None:
        return None

    def create_turn(self, turn, user_input: str = "") -> None:  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("database is locked")

    def end_turn(self, turn) -> None:  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("database is locked")

    def increment_session_tool_calls(self, session_id: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    def record_tool_execution(self, execution) -> None:  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("database is locked")


@pytest.mark.asyncio
async def test_processor_continues_when_sqlite_is_locked(tmp_path) -> None:
    config = ObservabilityConfig(enabled=True, tenant=TenantConfig("team", "project"))
    context = TelemetryContext.from_config(config, model="gpt-5-mini")
    exporter = SQLiteExporter(db_path=tmp_path / "telemetry.db", config=config)
    exporter._storage = LockingStorage()  # type: ignore[assignment]
    processor = ObservabilityProcessor(config, context, exporter=exporter)

    async def events() -> AsyncIterator[AgentEvent]:
        yield AgentEvent(type="tool_start", tool_name="bash", tool_call_id="tool1")
        yield AgentEvent(type="tool_end", tool_name="bash", tool_call_id="tool1", tool_result="ok")
        yield AgentEvent(
            type="turn_complete",
            usage={
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_reasoning_tokens": 0,
                "context_size": 0,
            },
        )

    await processor.start_session()
    observed = [event async for event in processor.wrap_turn(events(), "prompt")]
    await processor.end_session()

    assert len(observed) == 3
    assert context.metadata.get("telemetry_degraded") is True
    assert context.metadata.get("telemetry_write_retries", 0) > 0
    assert context.metadata.get("telemetry_write_errors", 0) > 0


def test_storage_connection_sets_sqlite_pragmas(tmp_path) -> None:
    storage = TelemetryStorage(tmp_path / "telemetry.db")
    with storage._connection() as conn:  # type: ignore[attr-defined]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]

    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) >= 5000
    assert int(foreign_keys) == 1


def test_read_only_storage_can_query_existing_db(tmp_path) -> None:
    db_path = tmp_path / "telemetry.db"
    writable = TelemetryStorage(db_path)
    context = TelemetryContext(
        team_id="team",
        project_id="project",
        session_id="session-1",
        model="gpt-5-mini",
    )
    writable.create_session(context)

    read_only = TelemetryStorage(db_path, read_only=True)
    sessions = read_only.list_sessions(limit=10)
    assert [s.session_id for s in sessions] == ["session-1"]


def test_read_only_storage_requires_existing_db(tmp_path) -> None:
    missing_db = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        TelemetryStorage(missing_db, read_only=True)
