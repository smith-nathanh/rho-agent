"""SQLite storage backend for telemetry data."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..context import TelemetryContext, TurnContext, ToolExecutionContext


SCHEMA_SQL = """
-- Sessions table: one row per agent invocation
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    agent_id TEXT,
    environment TEXT,
    profile TEXT,
    model TEXT NOT NULL,
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,
    status TEXT DEFAULT 'active',
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    total_reasoning_tokens INTEGER DEFAULT 0,
    total_tool_calls INTEGER DEFAULT 0,
    context_size INTEGER DEFAULT 0,
    metadata JSON
);

-- Turns table: one row per user input/response cycle
CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    turn_index INTEGER NOT NULL,
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    context_size INTEGER DEFAULT 0,
    user_input TEXT
);

-- Tool executions table: one row per tool call
CREATE TABLE IF NOT EXISTS tool_executions (
    execution_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    tool_name TEXT NOT NULL,
    arguments JSON,
    result TEXT,
    success BOOLEAN DEFAULT TRUE,
    error TEXT,
    duration_ms INTEGER DEFAULT 0,
    started_at TIMESTAMP NOT NULL
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_sessions_team_project ON sessions(team_id, project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_turns_session_id ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_executions_turn_id ON tool_executions(turn_id);
CREATE INDEX IF NOT EXISTS idx_tool_executions_tool_name ON tool_executions(tool_name);
"""


@dataclass
class SessionSummary:
    """Summary of a session for listing."""

    session_id: str
    team_id: str
    project_id: str
    model: str
    started_at: datetime
    ended_at: datetime | None
    status: str
    total_input_tokens: int
    total_output_tokens: int
    total_reasoning_tokens: int
    total_tool_calls: int
    context_size: int
    turn_count: int


@dataclass
class SessionDetail:
    """Detailed session information including turns and tool executions."""

    session_id: str
    team_id: str
    project_id: str
    agent_id: str | None
    environment: str | None
    profile: str | None
    model: str
    started_at: datetime
    ended_at: datetime | None
    status: str
    total_input_tokens: int
    total_output_tokens: int
    total_reasoning_tokens: int
    total_tool_calls: int
    context_size: int
    metadata: dict[str, Any]
    turns: list[dict[str, Any]]


@dataclass
class ToolStats:
    """Statistics for tool usage."""

    tool_name: str
    total_calls: int
    success_count: int
    failure_count: int
    avg_duration_ms: float
    total_duration_ms: int


@dataclass
class CostSummary:
    """Cost/token summary for a time period."""

    team_id: str
    project_id: str
    total_sessions: int
    total_input_tokens: int
    total_output_tokens: int
    total_reasoning_tokens: int
    total_tool_calls: int


class TelemetryStorage:
    """SQLite storage for telemetry data."""

    def __init__(self, db_path: str | Path) -> None:
        """Initialize storage with database path.

        Args:
            db_path: Path to SQLite database file.
        """
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Get a database connection."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        """Initialize database schema."""
        with self._connection() as conn:
            conn.executescript(SCHEMA_SQL)

    def _parse_timestamp(self, ts: str | datetime | None) -> datetime | None:
        """Parse timestamp from various formats."""
        if ts is None:
            return None
        if isinstance(ts, datetime):
            return ts
        try:
            # Try ISO format first
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            # Try SQLite format
            return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")

    # --- Session operations ---

    def create_session(self, context: TelemetryContext) -> None:
        """Create a new session record."""
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, team_id, project_id, agent_id, environment,
                    profile, model, started_at, status, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    context.session_id,
                    context.team_id,
                    context.project_id,
                    context.agent_id or None,
                    context.environment,
                    context.profile,
                    context.model,
                    context.started_at.isoformat(),
                    context.status,
                    json.dumps(context.metadata),
                ),
            )
            conn.commit()

    def update_session(self, context: TelemetryContext) -> None:
        """Update session with current state."""
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE sessions SET
                    ended_at = ?,
                    status = ?,
                    total_input_tokens = ?,
                    total_output_tokens = ?,
                    total_reasoning_tokens = ?,
                    total_tool_calls = ?,
                    context_size = ?,
                    metadata = ?
                WHERE session_id = ?
                """,
                (
                    context.ended_at.isoformat() if context.ended_at else None,
                    context.status,
                    context.total_input_tokens,
                    context.total_output_tokens,
                    context.total_reasoning_tokens,
                    context.total_tool_calls,
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
        """Mark a session as ended."""
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE sessions SET
                    ended_at = ?,
                    status = ?,
                    total_input_tokens = ?,
                    total_output_tokens = ?,
                    total_reasoning_tokens = ?,
                    total_tool_calls = ?
                WHERE session_id = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
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
        """Increment tool call count for a session (survives timeout)."""
        with self._connection() as conn:
            conn.execute(
                "UPDATE sessions SET total_tool_calls = total_tool_calls + ? WHERE session_id = ?",
                (count, session_id),
            )
            conn.commit()

    # --- Turn operations ---

    def create_turn(self, turn: TurnContext, user_input: str = "") -> None:
        """Create a new turn record."""
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO turns (
                    turn_id, session_id, turn_index, started_at, user_input
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    turn.turn_id,
                    turn.session_id,
                    turn.turn_index,
                    turn.started_at.isoformat(),
                    user_input,
                ),
            )
            conn.commit()

    def end_turn(self, turn: TurnContext) -> None:
        """Mark a turn as ended with final token counts."""
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE turns SET
                    ended_at = ?,
                    input_tokens = ?,
                    output_tokens = ?,
                    reasoning_tokens = ?,
                    context_size = ?
                WHERE turn_id = ?
                """,
                (
                    turn.ended_at.isoformat() if turn.ended_at else datetime.now(timezone.utc).isoformat(),
                    turn.input_tokens,
                    turn.output_tokens,
                    turn.reasoning_tokens,
                    turn.context_size,
                    turn.turn_id,
                ),
            )
            conn.commit()

    # --- Tool execution operations ---

    def record_tool_execution(self, execution: ToolExecutionContext) -> None:
        """Record a completed tool execution."""
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO tool_executions (
                    execution_id, turn_id, tool_name, arguments, result,
                    success, error, duration_ms, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    execution.started_at.isoformat(),
                ),
            )
            conn.commit()

    # --- Query operations ---

    def list_sessions(
        self,
        team_id: str | None = None,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionSummary]:
        """List sessions with optional filtering."""
        with self._connection() as conn:
            # Build query with filters
            conditions = []
            params: list[Any] = []

            if team_id:
                conditions.append("s.team_id = ?")
                params.append(team_id)
            if project_id:
                conditions.append("s.project_id = ?")
                params.append(project_id)
            if status:
                conditions.append("s.status = ?")
                params.append(status)

            where_clause = " AND ".join(conditions) if conditions else "1=1"

            query = f"""
                SELECT
                    s.session_id, s.team_id, s.project_id, s.model,
                    s.started_at, s.ended_at, s.status,
                    s.total_input_tokens, s.total_output_tokens, s.total_reasoning_tokens,
                    s.total_tool_calls, s.context_size,
                    COUNT(t.turn_id) as turn_count
                FROM sessions s
                LEFT JOIN turns t ON s.session_id = t.session_id
                WHERE {where_clause}
                GROUP BY s.session_id
                ORDER BY s.started_at DESC
                LIMIT ? OFFSET ?
            """
            params.extend([limit, offset])

            cursor = conn.execute(query, params)
            results = []
            for row in cursor:
                results.append(
                    SessionSummary(
                        session_id=row["session_id"],
                        team_id=row["team_id"],
                        project_id=row["project_id"],
                        model=row["model"],
                        started_at=self._parse_timestamp(row["started_at"]) or datetime.now(timezone.utc),
                        ended_at=self._parse_timestamp(row["ended_at"]),
                        status=row["status"],
                        total_input_tokens=row["total_input_tokens"] or 0,
                        total_output_tokens=row["total_output_tokens"] or 0,
                        total_reasoning_tokens=row["total_reasoning_tokens"] or 0,
                        total_tool_calls=row["total_tool_calls"] or 0,
                        context_size=row["context_size"] or 0,
                        turn_count=row["turn_count"] or 0,
                    )
                )
            return results

    def get_session_detail(self, session_id: str) -> SessionDetail | None:
        """Get detailed session information including turns and tool executions."""
        with self._connection() as conn:
            # Get session
            cursor = conn.execute(
                """
                SELECT * FROM sessions WHERE session_id = ?
                """,
                (session_id,),
            )
            session_row = cursor.fetchone()
            if not session_row:
                return None

            # Get turns with tool executions
            turns_query = """
                SELECT
                    t.turn_id, t.turn_index, t.started_at, t.ended_at,
                    t.input_tokens, t.output_tokens, t.reasoning_tokens, t.context_size, t.user_input
                FROM turns t
                WHERE t.session_id = ?
                ORDER BY t.turn_index
            """
            turns_cursor = conn.execute(turns_query, (session_id,))
            turns = []
            for turn_row in turns_cursor:
                # Get tool executions for this turn
                tools_cursor = conn.execute(
                    """
                    SELECT * FROM tool_executions
                    WHERE turn_id = ?
                    ORDER BY started_at
                    """,
                    (turn_row["turn_id"],),
                )
                tool_executions = [
                    {
                        "execution_id": tr["execution_id"],
                        "tool_name": tr["tool_name"],
                        "arguments": json.loads(tr["arguments"]) if tr["arguments"] else {},
                        "result": tr["result"],
                        "success": bool(tr["success"]),
                        "error": tr["error"],
                        "duration_ms": tr["duration_ms"],
                        "started_at": tr["started_at"],
                    }
                    for tr in tools_cursor
                ]

                turns.append(
                    {
                        "turn_id": turn_row["turn_id"],
                        "turn_index": turn_row["turn_index"],
                        "started_at": turn_row["started_at"],
                        "ended_at": turn_row["ended_at"],
                        "input_tokens": turn_row["input_tokens"] or 0,
                        "output_tokens": turn_row["output_tokens"] or 0,
                        "reasoning_tokens": turn_row["reasoning_tokens"] or 0,
                        "context_size": turn_row["context_size"] or 0,
                        "user_input": turn_row["user_input"],
                        "tool_executions": tool_executions,
                    }
                )

            return SessionDetail(
                session_id=session_row["session_id"],
                team_id=session_row["team_id"],
                project_id=session_row["project_id"],
                agent_id=session_row["agent_id"],
                environment=session_row["environment"],
                profile=session_row["profile"],
                model=session_row["model"],
                started_at=self._parse_timestamp(session_row["started_at"]) or datetime.now(timezone.utc),
                ended_at=self._parse_timestamp(session_row["ended_at"]),
                status=session_row["status"],
                total_input_tokens=session_row["total_input_tokens"] or 0,
                total_output_tokens=session_row["total_output_tokens"] or 0,
                total_reasoning_tokens=session_row["total_reasoning_tokens"] or 0,
                total_tool_calls=session_row["total_tool_calls"] or 0,
                context_size=session_row["context_size"] or 0,
                metadata=json.loads(session_row["metadata"]) if session_row["metadata"] else {},
                turns=turns,
            )

    def get_tool_stats(
        self,
        team_id: str | None = None,
        project_id: str | None = None,
        days: int = 30,
    ) -> list[ToolStats]:
        """Get tool usage statistics."""
        with self._connection() as conn:
            conditions = ["s.started_at >= datetime('now', ?)" ]
            params: list[Any] = [f"-{days} days"]

            if team_id:
                conditions.append("s.team_id = ?")
                params.append(team_id)
            if project_id:
                conditions.append("s.project_id = ?")
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

            cursor = conn.execute(query, params)
            return [
                ToolStats(
                    tool_name=row["tool_name"],
                    total_calls=row["total_calls"],
                    success_count=row["success_count"] or 0,
                    failure_count=row["failure_count"] or 0,
                    avg_duration_ms=row["avg_duration_ms"] or 0,
                    total_duration_ms=row["total_duration_ms"] or 0,
                )
                for row in cursor
            ]

    def get_cost_summary(
        self,
        team_id: str | None = None,
        project_id: str | None = None,
        days: int = 30,
    ) -> list[CostSummary]:
        """Get cost/token summary grouped by team and project."""
        with self._connection() as conn:
            conditions = ["started_at >= datetime('now', ?)"]
            params: list[Any] = [f"-{days} days"]

            if team_id:
                conditions.append("team_id = ?")
                params.append(team_id)
            if project_id:
                conditions.append("project_id = ?")
                params.append(project_id)

            where_clause = " AND ".join(conditions)

            query = f"""
                SELECT
                    team_id, project_id,
                    COUNT(*) as total_sessions,
                    SUM(total_input_tokens) as total_input_tokens,
                    SUM(total_output_tokens) as total_output_tokens,
                    SUM(total_reasoning_tokens) as total_reasoning_tokens,
                    SUM(total_tool_calls) as total_tool_calls
                FROM sessions
                WHERE {where_clause}
                GROUP BY team_id, project_id
                ORDER BY total_input_tokens + total_output_tokens DESC
            """

            cursor = conn.execute(query, params)
            return [
                CostSummary(
                    team_id=row["team_id"],
                    project_id=row["project_id"],
                    total_sessions=row["total_sessions"],
                    total_input_tokens=row["total_input_tokens"] or 0,
                    total_output_tokens=row["total_output_tokens"] or 0,
                    total_reasoning_tokens=row["total_reasoning_tokens"] or 0,
                    total_tool_calls=row["total_tool_calls"] or 0,
                )
                for row in cursor
            ]

    def get_active_sessions(self) -> list[SessionSummary]:
        """Get currently active sessions."""
        return self.list_sessions(status="active")
