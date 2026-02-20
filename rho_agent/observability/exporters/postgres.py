"""PostgreSQL exporter for telemetry data using async psycopg v3."""

from __future__ import annotations

import json
import logging
from typing import Any

from .base import Exporter
from ..config import ObservabilityConfig
from ..context import TelemetryContext, ToolExecutionContext, TurnContext

logger = logging.getLogger(__name__)
MAX_WRITE_RETRIES = 3
BASE_RETRY_DELAY_S = 0.05


class PostgresExporter(Exporter):
    """Exporter that persists telemetry data to PostgreSQL.

    Uses psycopg v3 AsyncConnection for non-blocking writes from the agent
    event loop (unlike SQLiteExporter which wraps sync calls via to_thread).
    """

    def __init__(self, config: ObservabilityConfig) -> None:
        self._dsn = config.backend.postgres.dsn
        self._conn: Any = None  # psycopg.AsyncConnection
        self._current_context: TelemetryContext | None = None

    async def _ensure_conn(self) -> Any:
        if self._conn is None or self._conn.closed:
            import psycopg

            self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        return self._conn

    async def start_session(self, context: TelemetryContext) -> None:
        self._current_context = context
        await self._run_write(
            "create_session",
            """
            INSERT INTO sessions (
                session_id, team_id, project_id, agent_id, environment,
                profile, model, started_at, status, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                context.session_id,
                context.team_id,
                context.project_id,
                context.agent_id or None,
                context.environment,
                context.profile,
                context.model,
                context.started_at,
                context.status,
                json.dumps(context.metadata),
            ),
        )

    async def end_session(self, context: TelemetryContext) -> None:
        await self._run_write(
            "update_session",
            """
            UPDATE sessions SET
                ended_at = %s, status = %s,
                total_input_tokens = %s, total_output_tokens = %s,
                total_reasoning_tokens = %s, total_tool_calls = %s,
                total_cost_usd = %s, context_size = %s, metadata = %s
            WHERE session_id = %s
            """,
            (
                context.ended_at,
                context.status,
                context.total_input_tokens,
                context.total_output_tokens,
                context.total_reasoning_tokens,
                context.total_tool_calls,
                context.total_cost_usd,
                context.context_size,
                json.dumps(context.metadata),
                context.session_id,
            ),
        )
        self._current_context = None

    async def start_turn(self, turn: TurnContext, user_input: str = "") -> None:
        await self._run_write(
            "create_turn",
            """
            INSERT INTO turns (turn_id, session_id, turn_index, started_at, user_input)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (turn.turn_id, turn.session_id, turn.turn_index, turn.started_at, user_input),
        )

    async def end_turn(self, turn: TurnContext) -> None:
        from datetime import datetime, timezone

        await self._run_write(
            "end_turn",
            """
            UPDATE turns SET
                ended_at = %s, input_tokens = %s, output_tokens = %s,
                reasoning_tokens = %s, cost_usd = %s, context_size = %s
            WHERE turn_id = %s
            """,
            (
                turn.ended_at or datetime.now(timezone.utc),
                turn.input_tokens,
                turn.output_tokens,
                turn.reasoning_tokens,
                turn.cost_usd,
                turn.context_size,
                turn.turn_id,
            ),
        )

    async def record_model_call(
        self,
        turn_id: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
    ) -> None:
        if self._current_context:
            self._current_context.record_tokens(input_tokens, output_tokens)

    async def record_tool_execution(self, execution: ToolExecutionContext) -> None:
        await self._run_write(
            "record_tool_execution",
            """
            INSERT INTO tool_executions (
                execution_id, turn_id, tool_name, arguments, result,
                success, error, duration_ms, started_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                execution.execution_id,
                execution.turn_id,
                execution.tool_name,
                json.dumps(execution.arguments),
                execution.result,
                execution.success,
                execution.error,
                execution.duration_ms,
                execution.started_at,
            ),
        )

    async def increment_tool_call(self, session_id: str) -> None:
        await self._run_write(
            "increment_tool_calls",
            "UPDATE sessions SET total_tool_calls = total_tool_calls + 1 WHERE session_id = %s",
            (session_id,),
        )

    async def close(self) -> None:
        if self._conn and not self._conn.closed:
            await self._conn.close()
            self._conn = None

    async def _run_write(self, op_name: str, sql: str, params: tuple[Any, ...]) -> None:
        """Execute a write with retry on transient errors."""
        import asyncio

        attempts = 0
        while True:
            try:
                conn = await self._ensure_conn()
                await conn.execute(sql, params)
                return
            except Exception as exc:
                if attempts < MAX_WRITE_RETRIES and self._is_transient(exc):
                    attempts += 1
                    self._record_retry()
                    # Force reconnect on next attempt
                    self._conn = None
                    await asyncio.sleep(BASE_RETRY_DELAY_S * attempts)
                    continue
                self._record_write_error(op_name, exc)
                return

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        try:
            import psycopg

            return isinstance(exc, psycopg.OperationalError)
        except ImportError:
            return False

    def _record_retry(self) -> None:
        if not self._current_context:
            return
        md = self._current_context.metadata
        md["telemetry_degraded"] = True
        md["telemetry_write_retries"] = md.get("telemetry_write_retries", 0) + 1

    def _record_write_error(self, op_name: str, exc: Exception) -> None:
        if self._current_context:
            md = self._current_context.metadata
            md["telemetry_degraded"] = True
            md["telemetry_write_errors"] = md.get("telemetry_write_errors", 0) + 1
        logger.warning("Telemetry write skipped for %s: %s", op_name, exc)
