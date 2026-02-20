"""SQLite exporter for telemetry data."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .base import Exporter
from ..config import ObservabilityConfig, DEFAULT_TELEMETRY_DB
from ..context import TelemetryContext, TurnContext, ToolExecutionContext
from ..storage.sqlite import TelemetryStorage

logger = logging.getLogger(__name__)
MAX_WRITE_RETRIES = 3
BASE_RETRY_DELAY_S = 0.05


class SQLiteExporter(Exporter):
    """Exporter that persists telemetry data to SQLite.

    This is the default exporter that requires no external dependencies.
    Data is stored locally and can be queried via the dashboard.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        config: ObservabilityConfig | None = None,
    ) -> None:
        """Initialize SQLite exporter.

        Args:
            db_path: Path to SQLite database. If not provided, uses config or default.
            config: Observability config to get database path from.
        """
        if db_path:
            resolved_path = Path(db_path).expanduser()
        elif config and config.backend.sqlite:
            resolved_path = Path(config.backend.sqlite.path).expanduser()
        else:
            resolved_path = DEFAULT_TELEMETRY_DB

        self._storage = TelemetryStorage(resolved_path)
        self._current_context: TelemetryContext | None = None

    @property
    def storage(self) -> TelemetryStorage:
        """Get the underlying storage for queries."""
        return self._storage

    async def start_session(self, context: TelemetryContext) -> None:
        """Create a new session record."""
        self._current_context = context
        await self._run_write("create_session", self._storage.create_session, context)

    async def end_session(self, context: TelemetryContext) -> None:
        """Update session with final state."""
        await self._run_write("update_session", self._storage.update_session, context)
        self._current_context = None

    async def start_turn(self, turn: TurnContext, user_input: str = "") -> None:
        """Create a new turn record."""
        await self._run_write("create_turn", self._storage.create_turn, turn, user_input)

    async def end_turn(self, turn: TurnContext) -> None:
        """Update turn with final token counts."""
        await self._run_write("end_turn", self._storage.end_turn, turn)

    async def record_model_call(
        self,
        turn_id: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
    ) -> None:
        """Record model call metrics.

        Note: Model calls are aggregated into turns in the SQLite schema.
        This method updates the running context but doesn't create a separate record.
        """
        if self._current_context:
            self._current_context.record_tokens(input_tokens, output_tokens)

    async def record_tool_execution(
        self,
        execution: ToolExecutionContext,
    ) -> None:
        """Record a tool execution."""
        await self._run_write(
            "record_tool_execution", self._storage.record_tool_execution, execution
        )

    async def increment_tool_call(self, session_id: str) -> None:
        """Increment session tool call counter."""
        await self._run_write(
            "increment_session_tool_calls",
            self._storage.increment_session_tool_calls,
            session_id,
        )

    async def flush(self) -> None:
        """SQLite auto-commits, so flush is a no-op."""
        pass

    async def close(self) -> None:
        """Clean up resources."""
        # SQLite connections are created per-operation, so nothing to close
        pass

    async def _run_write(self, op_name: str, func: Any, *args: Any) -> None:
        """Run a storage write as best-effort telemetry."""
        attempts = 0
        while True:
            try:
                # Run in thread pool since SQLite is sync.
                await asyncio.to_thread(func, *args)
                return
            except sqlite3.OperationalError as exc:
                if attempts < MAX_WRITE_RETRIES and self._is_lock_error(exc):
                    attempts += 1
                    self._record_retry()
                    await asyncio.sleep(BASE_RETRY_DELAY_S * attempts)
                    continue
                self._record_write_error(op_name, exc)
                return
            except Exception as exc:
                self._record_write_error(op_name, exc)
                return

    @staticmethod
    def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
        message = str(exc).lower()
        return "database is locked" in message or "database is busy" in message

    def _record_retry(self) -> None:
        if not self._current_context:
            return
        metadata = self._current_context.metadata
        metadata["telemetry_degraded"] = True
        metadata["telemetry_write_retries"] = metadata.get("telemetry_write_retries", 0) + 1

    def _record_write_error(self, op_name: str, exc: Exception) -> None:
        if self._current_context:
            metadata = self._current_context.metadata
            metadata["telemetry_degraded"] = True
            metadata["telemetry_write_errors"] = metadata.get("telemetry_write_errors", 0) + 1
        logger.warning("Telemetry write skipped for %s: %s", op_name, exc)


def create_exporter(config: ObservabilityConfig) -> Exporter:
    """Create an exporter based on configuration.

    Args:
        config: Observability configuration.

    Returns:
        Configured exporter instance.
    """
    from .base import NoOpExporter

    if not config.enabled:
        return NoOpExporter()

    backend_type = config.backend.type

    if backend_type == "sqlite":
        return SQLiteExporter(config=config)

    elif backend_type == "postgres":
        from .postgres import PostgresExporter

        return PostgresExporter(config=config)

    elif backend_type == "otlp":
        # OTLP exporter would be implemented here
        # For now, fall back to SQLite
        try:
            from .otlp import OTLPExporter

            return OTLPExporter(config=config)
        except ImportError:
            # OTLP dependencies not installed, fall back to SQLite
            return SQLiteExporter(config=config)

    else:
        # Unknown backend, use SQLite as fallback
        return SQLiteExporter(config=config)
