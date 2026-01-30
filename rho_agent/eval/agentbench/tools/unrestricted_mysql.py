"""MySQL handler for AgentBench evaluation tasks.

This handler executes all SQL via `docker exec` for full container isolation -
no MySQL ports are exposed to the host. This is more secure than the standard
MysqlHandler which connects via network.

Key differences from standard MysqlHandler:
1. Tool name: 'execute_sql' (AgentBench) vs 'mysql' (standard)
2. Execution: docker exec (isolated) vs network connection
3. Has calculate_table_hash() for mutation verification (eval-specific)
4. No approval required (sandboxed environment)

See runner.py for why eval uses custom handlers.
"""

import asyncio
import re
from typing import Any

from rho_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput
from rho_agent.tools.handlers.database import DEFAULT_ROW_LIMIT


class EvalMySQLHandler(ToolHandler):
    """MySQL handler for DBBench evaluation tasks.

    Executes all SQL via `docker exec` for full container isolation.
    No ports are exposed to the host, making this more secure than
    network-based connections.
    """

    def __init__(
        self,
        container_id: str,
        database: str,
        password: str = "evalpass",
        row_limit: int = DEFAULT_ROW_LIMIT,
    ) -> None:
        """Initialize the MySQL handler.

        Args:
            container_id: Docker container ID running MySQL
            database: Database name to use
            password: MySQL root password
            row_limit: Maximum number of rows to return in query results
        """
        self._container_id = container_id
        self._database = database
        self._password = password
        self._row_limit = row_limit

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

    def close(self) -> None:
        """No-op for docker exec based handler."""
        pass

    async def _exec_sql(self, sql: str, database: str | None = None) -> tuple[int, str, str]:
        """Execute SQL via docker exec.

        Args:
            sql: SQL query to execute
            database: Database to use (defaults to self._database)

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        db = database or self._database
        cmd = [
            "docker",
            "exec",
            self._container_id,
            "mysql",
            "-u",
            "root",
            f"-p{self._password}",
            "-D",
            db,
            "-e",
            sql,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await proc.communicate()
            return (
                proc.returncode or 0,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            )
        except FileNotFoundError:
            return (1, "", "Docker command not found. Is Docker installed?")
        except Exception as e:
            return (1, "", f"Error executing docker command: {e}")

    def _parse_mysql_output(self, output: str) -> tuple[list[str], list[list[str]]]:
        """Parse MySQL tabular output into columns and rows.

        Args:
            output: Raw MySQL output (tab-separated with header row)

        Returns:
            Tuple of (column_names, rows)
        """
        lines = output.strip().split("\n")
        if not lines:
            return [], []

        # First line is headers (tab-separated)
        columns = lines[0].split("\t")

        # Remaining lines are data rows
        rows = []
        for line in lines[1:]:
            if line.strip():
                rows.append(line.split("\t"))

        return columns, rows

    def _format_rows(self, columns: list[str], rows: list[list[str]]) -> str:
        """Format rows for display."""
        if not rows:
            return "No results returned."

        # Truncate if needed
        truncated = len(rows) > self._row_limit
        display_rows = rows[: self._row_limit]

        # Calculate column widths (cap at 50 for readability)
        widths = [min(len(col), 50) for col in columns]
        for row in display_rows:
            for i, val in enumerate(row):
                if i < len(widths):
                    widths[i] = min(max(widths[i], len(str(val))), 50)

        # Build output
        lines = []
        lines.append(" | ".join(col.ljust(widths[i])[:widths[i]] for i, col in enumerate(columns)))
        lines.append("-+-".join("-" * w for w in widths))
        for row in display_rows:
            lines.append(" | ".join(
                str(v).ljust(widths[i])[:widths[i]] if i < len(widths) else str(v)
                for i, v in enumerate(row)
            ))

        if truncated:
            lines.append(f"... ({len(rows) - self._row_limit} more rows)")

        return "\n".join(lines)

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Execute the SQL query via docker exec."""
        sql = invocation.arguments.get("sql", "").strip()

        if not sql:
            return ToolOutput(content="No SQL query provided", success=False)

        returncode, stdout, stderr = await self._exec_sql(sql)

        # Filter out password warning from stderr
        stderr_filtered = "\n".join(
            line for line in stderr.split("\n")
            if "Using a password on the command line" not in line
        ).strip()

        if returncode != 0:
            error_msg = stderr_filtered or stdout or "Unknown error"
            return ToolOutput(
                content=f"SQL error: {error_msg}",
                success=False,
            )

        # Check if this looks like a SELECT result (has output with columns)
        if stdout.strip():
            columns, rows = self._parse_mysql_output(stdout)
            if columns:
                content = self._format_rows(columns, rows)
                return ToolOutput(
                    content=content,
                    success=True,
                    metadata={
                        "columns": columns,
                        "row_count": min(len(rows), self._row_limit),
                        "truncated": len(rows) > self._row_limit,
                    },
                )

        # Non-SELECT query (INSERT, UPDATE, DELETE) or empty result
        # Try to extract rows affected from output
        rows_affected = 0
        if match := re.search(r"(\d+) rows? affected", stdout + stderr):
            rows_affected = int(match.group(1))

        return ToolOutput(
            content=f"Query executed successfully. Rows affected: {rows_affected}",
            success=True,
            metadata={"rows_affected": rows_affected},
        )

    async def calculate_table_hash(self, table_info: dict, table_name: str) -> str | None:
        """Calculate MD5 hash of table state for mutation verification.

        Uses the same algorithm as AgentBench to compute a hash of all rows
        in the table, which can be compared against the pre-computed answer_md5.

        This is an eval-specific feature not available in the standard MysqlHandler.

        Args:
            table_info: Dict with 'columns' list containing column info
            table_name: Name of the table to hash

        Returns:
            MD5 hash string, or None if calculation fails
        """
        try:
            # Build column list for CONCAT_WS
            columns = [f"`{col['name']}`" for col in table_info["columns"]]
            columns_str = ", ".join(columns)

            # AgentBench hash query
            query = (
                f"SELECT MD5(GROUP_CONCAT(rowhash ORDER BY rowhash)) AS hash "
                f"FROM ("
                f"SELECT SUBSTRING(MD5(CONCAT_WS(',', {columns_str})), 1, 5) AS rowhash "
                f"FROM `{table_name}`"
                f") AS sub"
            )

            returncode, stdout, stderr = await self._exec_sql(query)

            if returncode != 0:
                return None

            # Parse the result - should be "hash\n<value>"
            columns, rows = self._parse_mysql_output(stdout)
            if rows and rows[0]:
                return rows[0][0]

            return None

        except Exception:
            return None


# Backwards compatibility alias
UnrestrictedMySQLHandler = EvalMySQLHandler
