"""SQLite exporter for telemetry data."""

import asyncio
from pathlib import Path
from typing import Any

from .base import Exporter
from ..config import ObservabilityConfig, DEFAULT_TELEMETRY_DB
from ..context import TelemetryContext, TurnContext, ToolExecutionContext
from ..storage.sqlite import TelemetryStorage


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
        # Run in thread pool since SQLite is sync
        await asyncio.to_thread(self._storage.create_session, context)

    async def end_session(self, context: TelemetryContext) -> None:
        """Update session with final state."""
        await asyncio.to_thread(self._storage.update_session, context)
        self._current_context = None

    async def start_turn(self, turn: TurnContext, user_input: str = "") -> None:
        """Create a new turn record."""
        await asyncio.to_thread(self._storage.create_turn, turn, user_input)

    async def end_turn(self, turn: TurnContext) -> None:
        """Update turn with final token counts."""
        await asyncio.to_thread(self._storage.end_turn, turn)

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
        await asyncio.to_thread(self._storage.record_tool_execution, execution)

    async def flush(self) -> None:
        """SQLite auto-commits, so flush is a no-op."""
        pass

    async def close(self) -> None:
        """Clean up resources."""
        # SQLite connections are created per-operation, so nothing to close
        pass


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
