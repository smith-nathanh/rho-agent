"""Tool handlers for BIRD-Bench evaluation.

Two tools are provided to the agent:
- execute_sql: Run SQL queries against the task database (read-write on a copy)
- submit_sql: Submit the final SQL query for evaluation
"""

import asyncio
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ro_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput
from ro_agent.tools.handlers.database import format_rows, DEFAULT_ROW_LIMIT

# Per-query timeout for agent exploration queries (seconds).
_QUERY_TIMEOUT = 30


class BirdSqliteHandler(ToolHandler):
    """SQLite handler for BIRD-Bench agent exploration.

    The agent uses this to explore schema, sample data, and test queries.
    Operates on a COPY of the database so mutations cannot corrupt the original.
    """

    def __init__(
        self,
        db_path: str | Path,
        row_limit: int = DEFAULT_ROW_LIMIT,
    ) -> None:
        self._db_path = Path(db_path)
        self._row_limit = row_limit
        self._connection: sqlite3.Connection | None = None

    @property
    def name(self) -> str:
        return "execute_sql"

    @property
    def description(self) -> str:
        return (
            "Execute a SQL query against the SQLite database. "
            "Use this to explore the schema (SELECT name FROM sqlite_master WHERE type='table'), "
            "inspect table structures (PRAGMA table_info(table_name)), "
            "sample data, and test your queries."
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
        return False

    def _get_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
            )
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _execute_sync(self, sql: str) -> ToolOutput:
        """Execute SQL synchronously with a per-query timeout.

        Uses sqlite3 progress_handler to abort queries that exceed the timeout.
        Called from a worker thread via asyncio.to_thread().
        """
        conn = self._get_connection()

        start = time.monotonic()

        def _check_timeout() -> int:
            if time.monotonic() - start > _QUERY_TIMEOUT:
                return 1  # non-zero aborts the query
            return 0

        conn.set_progress_handler(_check_timeout, 1000)

        try:
            cursor = conn.cursor()
            cursor.execute(sql)

            if cursor.description is not None:
                columns = [col[0] for col in cursor.description]
                rows = cursor.fetchall()

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
                conn.commit()
                rows_affected = cursor.rowcount
                return ToolOutput(
                    content=f"Query executed successfully. Rows affected: {rows_affected}",
                    success=True,
                    metadata={"rows_affected": rows_affected},
                )

        except sqlite3.OperationalError as e:
            if "interrupted" in str(e).lower():
                return ToolOutput(
                    content=f"Query timed out after {_QUERY_TIMEOUT}s",
                    success=False,
                )
            return ToolOutput(content=f"SQL error: {e}", success=False)
        except sqlite3.Error as e:
            return ToolOutput(content=f"SQL error: {e}", success=False)
        except Exception as e:
            return ToolOutput(content=f"Error executing query: {e}", success=False)
        finally:
            conn.set_progress_handler(None, 0)

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        sql = invocation.arguments.get("sql", "").strip()

        if not sql:
            return ToolOutput(content="No SQL query provided", success=False)

        return await asyncio.to_thread(self._execute_sync, sql)


class SubmitSqlHandler(ToolHandler):
    """Tool for the agent to submit its final SQL answer.

    The submitted SQL is captured via callback and later evaluated
    against the gold SQL by comparing execution results.
    """

    def __init__(
        self,
        on_submit: Callable[[str], None] | None = None,
    ) -> None:
        self._on_submit = on_submit
        self._submitted_sql: str | None = None
        self._is_submitted = False

    @property
    def name(self) -> str:
        return "submit_sql"

    @property
    def description(self) -> str:
        return (
            "Submit your final SQL query to answer the question. "
            "The query will be executed and its results compared against the "
            "expected answer. Make sure your query is correct before submitting. "
            "Only submit a single SELECT query."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "Your final SQL query that answers the question",
                },
            },
            "required": ["sql"],
        }

    @property
    def requires_approval(self) -> bool:
        return False

    @property
    def submitted_sql(self) -> str | None:
        return self._submitted_sql

    @property
    def is_submitted(self) -> bool:
        return self._is_submitted

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        sql = invocation.arguments.get("sql", "").strip()

        if not sql:
            return ToolOutput(
                content="No SQL provided. Please provide your final SQL query.",
                success=False,
            )

        self._submitted_sql = sql
        self._is_submitted = True

        if self._on_submit:
            self._on_submit(sql)

        return ToolOutput(
            content=f"SQL submitted for evaluation: {sql}",
            success=True,
            metadata={"sql": sql},
        )
