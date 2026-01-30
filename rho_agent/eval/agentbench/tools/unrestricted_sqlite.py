"""SQLite handler for AgentBench evaluation tasks.

This is an adapter that provides the AgentBench-compatible 'execute_sql' interface
while leveraging the core SqliteHandler implementation. The key differences from
the standard SqliteHandler are:

1. Tool name: 'execute_sql' (AgentBench) vs 'sqlite' (standard)
2. Parameters: Simple {sql: string} vs operation-based {operation, sql, ...}
3. No approval required (sandboxed environment)
4. Mutations allowed (readonly=False)

See runner.py for why eval uses custom handlers.
"""

import sqlite3
from pathlib import Path
from typing import Any

from rho_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput
from rho_agent.tools.handlers.database import format_rows, DEFAULT_ROW_LIMIT


class EvalSqliteHandler(ToolHandler):
    """SQLite handler with AgentBench-compatible interface.

    Wraps SQLite database access with the 'execute_sql' tool interface
    expected by AgentBench evaluation tasks.
    """

    def __init__(
        self,
        db_path: str | Path,
        row_limit: int = DEFAULT_ROW_LIMIT,
    ) -> None:
        """Initialize the eval SQLite handler.

        Args:
            db_path: Path to the SQLite database file
            row_limit: Maximum number of rows to return in query results
        """
        self._db_path = Path(db_path)
        self._row_limit = row_limit
        self._connection: sqlite3.Connection | None = None

    @property
    def name(self) -> str:
        return "execute_sql"

    @property
    def description(self) -> str:
        return (
            "Execute a SQL query against the database. "
            "You can run SELECT queries to retrieve data, or INSERT/UPDATE/DELETE "
            "to modify the database. Returns query results or confirmation of changes."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "The SQL query to execute",
                },
            },
            "required": ["sql"],
        }

    @property
    def requires_approval(self) -> bool:
        return False  # No approval needed for sandboxed eval tasks

    def _get_connection(self) -> sqlite3.Connection:
        """Get or create database connection (read-write mode)."""
        if self._connection is None:
            self._connection = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
            )
        return self._connection

    def close(self) -> None:
        """Close the database connection."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Execute the SQL query."""
        sql = invocation.arguments.get("sql", "").strip()

        if not sql:
            return ToolOutput(content="No SQL query provided", success=False)

        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(sql)

            # Check if this is a SELECT query (has results)
            if cursor.description is not None:
                columns = [col[0] for col in cursor.description]
                rows = cursor.fetchall()

                # Limit rows and format using shared utility
                display_rows = rows[: self._row_limit + 1]
                content = format_rows(columns, display_rows, self._row_limit)

                return ToolOutput(
                    content=content,
                    success=True,
                    metadata={
                        "columns": columns,
                        "row_count": min(len(rows), self._row_limit),
                        "truncated": len(rows) > self._row_limit,
                    },
                )
            else:
                # Non-SELECT query (INSERT, UPDATE, DELETE)
                conn.commit()
                rows_affected = cursor.rowcount

                return ToolOutput(
                    content=f"Query executed successfully. Rows affected: {rows_affected}",
                    success=True,
                    metadata={"rows_affected": rows_affected},
                )

        except sqlite3.Error as e:
            return ToolOutput(
                content=f"SQL error: {e}",
                success=False,
            )
        except Exception as e:
            return ToolOutput(
                content=f"Error executing query: {e}",
                success=False,
            )


# Backwards compatibility alias
UnrestrictedSqliteHandler = EvalSqliteHandler
