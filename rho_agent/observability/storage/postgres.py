"""PostgreSQL storage backend for telemetry data."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..context import TelemetryContext, ToolExecutionContext, TurnContext
from .protocol import CostSummary, SessionDetail, SessionSummary, ToolStats

logger = logging.getLogger(__name__)

_SCHEMA_SQL_PATH = Path(__file__).parent / "postgres_schema.sql"


class PostgresTelemetryStore:
    """PostgreSQL storage backend satisfying the TelemetryStore protocol.

    Uses psycopg v3 with connection pooling via psycopg_pool.
    """

    def __init__(self, dsn: str, min_size: int = 2, max_size: int = 10) -> None:
        import psycopg_pool

        self._pool = psycopg_pool.ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, open=True
        )
        self._init_schema()

    def _init_schema(self) -> None:
        """Auto-create tables on first connect."""
        schema_sql = _SCHEMA_SQL_PATH.read_text()
        with self._pool.connection() as conn:
            conn.execute(schema_sql)
            conn.commit()

    def close(self) -> None:
        self._pool.close()

    # ── Session operations ──────────────────────────────────────────

    def create_session(self, context: TelemetryContext) -> None:
        with self._pool.connection() as conn:
            conn.execute(
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
            conn.commit()

    def update_session(self, context: TelemetryContext) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE sessions SET
                    ended_at = %s,
                    status = %s,
                    total_input_tokens = %s,
                    total_output_tokens = %s,
                    total_reasoning_tokens = %s,
                    total_tool_calls = %s,
                    total_cost_usd = %s,
                    context_size = %s,
                    metadata = %s
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
            conn.commit()

    def end_session(
        self,
        session_id: str,
        status: str = "completed",
        input_tokens: int = 0,
        output_tokens: int = 0,
        reasoning_tokens: int = 0,
        tool_calls: int = 0,
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE sessions SET
                    ended_at = %s,
                    status = %s,
                    total_input_tokens = %s,
                    total_output_tokens = %s,
                    total_reasoning_tokens = %s,
                    total_tool_calls = %s
                WHERE session_id = %s
                """,
                (
                    datetime.now(timezone.utc),
                    status,
                    input_tokens,
                    output_tokens,
                    reasoning_tokens,
                    tool_calls,
                    session_id,
                ),
            )
            conn.commit()

    def increment_session_tool_calls(self, session_id: str, count: int = 1) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE sessions SET total_tool_calls = total_tool_calls + %s WHERE session_id = %s",
                (count, session_id),
            )
            conn.commit()

    # ── Turn operations ─────────────────────────────────────────────

    def create_turn(self, turn: TurnContext, user_input: str = "") -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO turns (
                    turn_id, session_id, turn_index, started_at, user_input
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    turn.turn_id,
                    turn.session_id,
                    turn.turn_index,
                    turn.started_at,
                    user_input,
                ),
            )
            conn.commit()

    def end_turn(self, turn: TurnContext) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE turns SET
                    ended_at = %s,
                    input_tokens = %s,
                    output_tokens = %s,
                    reasoning_tokens = %s,
                    cost_usd = %s,
                    context_size = %s
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
            conn.commit()

    # ── Tool execution operations ───────────────────────────────────

    def record_tool_execution(self, execution: ToolExecutionContext) -> None:
        with self._pool.connection() as conn:
            conn.execute(
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
            conn.commit()

    # ── Query operations ────────────────────────────────────────────

    def _parse_timestamp(self, ts: Any) -> datetime | None:
        if ts is None:
            return None
        if isinstance(ts, datetime):
            return ts
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return None

    def list_sessions(
        self,
        team_id: str | None = None,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionSummary]:
        conditions: list[str] = []
        params: list[Any] = []

        if team_id:
            conditions.append("s.team_id = %s")
            params.append(team_id)
        if project_id:
            conditions.append("s.project_id = %s")
            params.append(project_id)
        if status:
            conditions.append("s.status = %s")
            params.append(status)

        where_clause = " AND ".join(conditions) if conditions else "TRUE"

        query = f"""
            SELECT
                s.session_id, s.team_id, s.project_id, s.model,
                s.started_at, s.ended_at, s.status,
                s.total_input_tokens, s.total_output_tokens, s.total_reasoning_tokens,
                s.total_tool_calls, s.total_cost_usd, s.context_size,
                COUNT(t.turn_id) as turn_count
            FROM sessions s
            LEFT JOIN turns t ON s.session_id = t.session_id
            WHERE {where_clause}
            GROUP BY s.session_id
            ORDER BY s.started_at DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])

        with self._pool.connection() as conn:
            cur = conn.execute(query, params)
            rows = cur.fetchall()
            # Get column names from description
            columns = [desc[0] for desc in cur.description] if cur.description else []

        results: list[SessionSummary] = []
        for row in rows:
            r = dict(zip(columns, row))
            results.append(
                SessionSummary(
                    session_id=r["session_id"],
                    team_id=r["team_id"],
                    project_id=r["project_id"],
                    model=r["model"],
                    started_at=self._parse_timestamp(r["started_at"])
                    or datetime.now(timezone.utc),
                    ended_at=self._parse_timestamp(r["ended_at"]),
                    status=r["status"],
                    total_input_tokens=r["total_input_tokens"] or 0,
                    total_output_tokens=r["total_output_tokens"] or 0,
                    total_reasoning_tokens=r["total_reasoning_tokens"] or 0,
                    total_tool_calls=r["total_tool_calls"] or 0,
                    total_cost_usd=r["total_cost_usd"] or 0.0,
                    context_size=r["context_size"] or 0,
                    turn_count=r["turn_count"] or 0,
                )
            )
        return results

    def count_sessions(self, status: str | None = None) -> int:
        with self._pool.connection() as conn:
            if status:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE status = %s", (status,)
                )
            else:
                cur = conn.execute("SELECT COUNT(*) FROM sessions")
            row = cur.fetchone()
            return row[0] if row else 0

    def get_session_detail(self, session_id: str) -> SessionDetail | None:
        with self._pool.connection() as conn:
            cur = conn.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
            session_row = cur.fetchone()
            if not session_row:
                return None
            s_cols = [desc[0] for desc in cur.description] if cur.description else []
            s = dict(zip(s_cols, session_row))

            # Get turns
            cur = conn.execute(
                """
                SELECT turn_id, turn_index, started_at, ended_at,
                       input_tokens, output_tokens, reasoning_tokens, cost_usd, context_size, user_input
                FROM turns WHERE session_id = %s ORDER BY turn_index
                """,
                (session_id,),
            )
            t_cols = [desc[0] for desc in cur.description] if cur.description else []
            turn_rows = cur.fetchall()

            turns: list[dict[str, Any]] = []
            for turn_row in turn_rows:
                t = dict(zip(t_cols, turn_row))
                # Get tool executions for this turn
                cur2 = conn.execute(
                    "SELECT * FROM tool_executions WHERE turn_id = %s ORDER BY started_at",
                    (t["turn_id"],),
                )
                te_cols = [desc[0] for desc in cur2.description] if cur2.description else []
                tool_executions = []
                for te_row in cur2.fetchall():
                    te = dict(zip(te_cols, te_row))
                    args = te.get("arguments")
                    if isinstance(args, str):
                        args = json.loads(args)
                    tool_executions.append(
                        {
                            "execution_id": te["execution_id"],
                            "tool_name": te["tool_name"],
                            "arguments": args or {},
                            "result": te.get("result"),
                            "success": bool(te.get("success")),
                            "error": te.get("error"),
                            "duration_ms": te.get("duration_ms", 0),
                            "started_at": str(te.get("started_at", "")),
                        }
                    )

                turns.append(
                    {
                        "turn_id": t["turn_id"],
                        "turn_index": t["turn_index"],
                        "started_at": str(t.get("started_at", "")),
                        "ended_at": str(t["ended_at"]) if t.get("ended_at") else None,
                        "input_tokens": t.get("input_tokens") or 0,
                        "output_tokens": t.get("output_tokens") or 0,
                        "reasoning_tokens": t.get("reasoning_tokens") or 0,
                        "cost_usd": t.get("cost_usd") or 0.0,
                        "context_size": t.get("context_size") or 0,
                        "user_input": t.get("user_input"),
                        "tool_executions": tool_executions,
                    }
                )

            metadata_raw = s.get("metadata")
            if isinstance(metadata_raw, str):
                metadata = json.loads(metadata_raw)
            elif isinstance(metadata_raw, dict):
                metadata = metadata_raw
            else:
                metadata = {}

            return SessionDetail(
                session_id=s["session_id"],
                team_id=s["team_id"],
                project_id=s["project_id"],
                agent_id=s.get("agent_id"),
                environment=s.get("environment"),
                profile=s.get("profile"),
                model=s["model"],
                started_at=self._parse_timestamp(s["started_at"])
                or datetime.now(timezone.utc),
                ended_at=self._parse_timestamp(s.get("ended_at")),
                status=s["status"],
                total_input_tokens=s.get("total_input_tokens") or 0,
                total_output_tokens=s.get("total_output_tokens") or 0,
                total_reasoning_tokens=s.get("total_reasoning_tokens") or 0,
                total_tool_calls=s.get("total_tool_calls") or 0,
                total_cost_usd=s.get("total_cost_usd") or 0.0,
                context_size=s.get("context_size") or 0,
                metadata=metadata,
                turns=turns,
            )

    def get_tool_stats(
        self,
        team_id: str | None = None,
        project_id: str | None = None,
        days: int = 30,
    ) -> list[ToolStats]:
        conditions = ["s.started_at >= NOW() - INTERVAL '%s days'"]
        params: list[Any] = [days]

        if team_id:
            conditions.append("s.team_id = %s")
            params.append(team_id)
        if project_id:
            conditions.append("s.project_id = %s")
            params.append(project_id)

        where_clause = " AND ".join(conditions)

        query = f"""
            SELECT
                te.tool_name,
                COUNT(*) as total_calls,
                SUM(CASE WHEN te.success THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN NOT te.success THEN 1 ELSE 0 END) as failure_count,
                AVG(te.duration_ms) as avg_duration_ms,
                SUM(te.duration_ms) as total_duration_ms
            FROM tool_executions te
            JOIN turns t ON te.turn_id = t.turn_id
            JOIN sessions s ON t.session_id = s.session_id
            WHERE {where_clause}
            GROUP BY te.tool_name
            ORDER BY total_calls DESC
        """

        with self._pool.connection() as conn:
            cur = conn.execute(query, params)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            return [
                ToolStats(
                    tool_name=r["tool_name"],
                    total_calls=r["total_calls"],
                    success_count=r["success_count"] or 0,
                    failure_count=r["failure_count"] or 0,
                    avg_duration_ms=float(r["avg_duration_ms"] or 0),
                    total_duration_ms=r["total_duration_ms"] or 0,
                )
                for row in cur.fetchall()
                for r in [dict(zip(columns, row))]
            ]

    def get_cost_summary(
        self,
        team_id: str | None = None,
        project_id: str | None = None,
        days: int = 30,
    ) -> list[CostSummary]:
        conditions = ["started_at >= NOW() - INTERVAL '%s days'"]
        params: list[Any] = [days]

        if team_id:
            conditions.append("team_id = %s")
            params.append(team_id)
        if project_id:
            conditions.append("project_id = %s")
            params.append(project_id)

        where_clause = " AND ".join(conditions)

        query = f"""
            SELECT
                team_id, project_id,
                COUNT(*) as total_sessions,
                SUM(total_input_tokens) as total_input_tokens,
                SUM(total_output_tokens) as total_output_tokens,
                SUM(total_reasoning_tokens) as total_reasoning_tokens,
                SUM(total_tool_calls) as total_tool_calls,
                SUM(total_cost_usd) as total_cost_usd
            FROM sessions
            WHERE {where_clause}
            GROUP BY team_id, project_id
            ORDER BY (SUM(total_input_tokens) + SUM(total_output_tokens)) DESC
        """

        with self._pool.connection() as conn:
            cur = conn.execute(query, params)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            return [
                CostSummary(
                    team_id=r["team_id"],
                    project_id=r["project_id"],
                    total_sessions=r["total_sessions"],
                    total_input_tokens=r["total_input_tokens"] or 0,
                    total_output_tokens=r["total_output_tokens"] or 0,
                    total_reasoning_tokens=r["total_reasoning_tokens"] or 0,
                    total_tool_calls=r["total_tool_calls"] or 0,
                    total_cost_usd=r["total_cost_usd"] or 0.0,
                )
                for row in cur.fetchall()
                for r in [dict(zip(columns, row))]
            ]

    def get_active_sessions(self) -> list[SessionSummary]:
        return self.list_sessions(status="active")
