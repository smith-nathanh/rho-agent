"""Base class for database handlers."""

from __future__ import annotations

import csv
import re
from abc import abstractmethod
from pathlib import Path
from typing import Any

from ...config.databases import DatabaseConfig
from ..base import ToolHandler, ToolInvocation, ToolOutput


# SQL patterns that indicate write operations (case-insensitive)
MUTATION_PATTERNS = [
    r"\bINSERT\b",
    r"\bUPDATE\b",
    r"\bDELETE\b",
    r"\bDROP\b",
    r"\bCREATE\b",
    r"\bALTER\b",
    r"\bTRUNCATE\b",
    r"\bMERGE\b",
    r"\bGRANT\b",
    r"\bREVOKE\b",
    r"\bEXEC\b",
    r"\bEXECUTE\b",
    r"\bCALL\b",
]

MUTATION_RE = re.compile("|".join(MUTATION_PATTERNS), re.IGNORECASE)

DEFAULT_ROW_LIMIT = 100


def is_read_only_sql(sql: str) -> tuple[bool, str]:
    """Check if SQL is read-only. Returns (is_safe, reason)."""
    # Strip comments and normalize whitespace
    cleaned = re.sub(r"--.*$", "", sql, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    cleaned = " ".join(cleaned.split())

    if MUTATION_RE.search(cleaned):
        match = MUTATION_RE.search(cleaned)
        return False, f"Query contains mutation keyword: {match.group()}"

    return True, ""


def format_rows(columns: list[str], rows: list[tuple], max_rows: int) -> str:
    """Format query results as a readable ASCII table."""
    if not rows:
        return "(no rows returned)"

    # Calculate column widths
    widths = [len(str(col)) for col in columns]
    for row in rows[:max_rows]:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val) if val is not None else "NULL"))

    # Cap widths at 50 chars for readability
    widths = [min(w, 50) for w in widths]

    # Build header
    header = " | ".join(str(col).ljust(widths[i])[: widths[i]] for i, col in enumerate(columns))
    separator = "-+-".join("-" * w for w in widths)

    # Build rows
    lines = [header, separator]
    for row in rows[:max_rows]:
        line = " | ".join(
            (str(val) if val is not None else "NULL").ljust(widths[i])[: widths[i]]
            for i, val in enumerate(row)
        )
        lines.append(line)

    if len(rows) > max_rows:
        lines.append(f"... ({len(rows) - max_rows} more rows)")

    return "\n".join(lines)


class DatabaseHandler(ToolHandler):
    """Base class for database handlers with configurable readonly mode.

    Subclasses implement connection management and catalog queries
    for their specific database systems.

    By default, the handler operates in readonly mode, blocking mutation queries.
    When readonly=False (for eval/sandbox scenarios), mutations are allowed.

    Multi-database support:
    - Pass `configs` list to support multiple databases of the same type
    - Each tool call must include `database` parameter to select which DB
    - Connections are cached by alias for efficiency
    """

    def __init__(
        self,
        configs: list["DatabaseConfig"],
        row_limit: int = DEFAULT_ROW_LIMIT,
        readonly: bool = True,
        requires_approval: bool = True,
    ) -> None:
        self._configs: dict[str, DatabaseConfig] = {c.alias: c for c in configs}
        self._connections: dict[str, Any] = {}  # alias -> connection
        self._row_limit = row_limit
        self._readonly = readonly
        self._requires_approval = requires_approval

    def close(self) -> None:
        """Close all database connections."""
        for alias in list(self._connections.keys()):
            self._close_connection(alias)

    def _close_connection(self, alias: str) -> None:
        """Close a specific connection by alias. Override in subclasses if needed."""
        conn = self._connections.pop(alias, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def __enter__(self) -> "DatabaseHandler":
        """Context manager entry - returns self."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit - ensures connection is closed."""
        self.close()

    def __del__(self) -> None:
        """Destructor - safety net to close connection on garbage collection."""
        try:
            self.close()
        except Exception:
            pass  # Ignore errors during cleanup

    @property
    @abstractmethod
    def db_type(self) -> str:
        """Database type identifier (e.g., 'oracle', 'sqlite', 'vertica')."""
        ...

    @property
    def name(self) -> str:
        return self.db_type

    @property
    def requires_approval(self) -> bool:
        return self._requires_approval

    @property
    def description(self) -> str:
        mode_desc = "read-only" if self._readonly else "full"
        db_list = ", ".join(sorted(self._configs.keys())) if self._configs else "none configured"
        return (
            f"Query {self.db_type.title()} databases [{db_list}]. "
            f"Operations: 'list_tables' to see tables, 'describe' for schema, "
            f"'query' for SQL, 'export_query' to export to CSV. "
            f"Access: {mode_desc}."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        # Build database field with enum of available aliases
        aliases = sorted(self._configs.keys()) if self._configs else []
        database_field: dict[str, Any] = {
            "type": "string",
            "description": "Database alias to query",
        }
        if aliases:
            database_field["enum"] = aliases

        # Only require database param if multiple databases configured
        required = ["operation"] if len(aliases) <= 1 else ["database", "operation"]

        return {
            "type": "object",
            "properties": {
                "database": database_field,
                "operation": {
                    "type": "string",
                    "enum": ["query", "list_tables", "describe", "export_query"],
                    "description": "Operation to perform",
                },
                "sql": {
                    "type": "string",
                    "description": "SQL query to execute (for 'query' operation)",
                },
                "table_pattern": {
                    "type": "string",
                    "description": "Table name pattern for filtering (for 'list_tables')",
                },
                "table_name": {
                    "type": "string",
                    "description": "Table name to describe (for 'describe')",
                },
                "schema": {
                    "type": "string",
                    "description": "Schema/owner name (optional)",
                },
                "row_limit": {
                    "type": "integer",
                    "description": f"Max rows to return (default: {DEFAULT_ROW_LIMIT})",
                },
                "export_path": {
                    "type": "string",
                    "description": "Absolute path for export file (required for export_query)",
                },
                "format": {
                    "type": "string",
                    "enum": ["csv"],
                    "default": "csv",
                    "description": "Export format (default: csv)",
                },
            },
            "required": required,
        }

    def _get_config(self, alias: str) -> "DatabaseConfig":
        """Get config for a database alias."""
        if alias not in self._configs:
            available = ", ".join(sorted(self._configs.keys())) or "none"
            raise ValueError(f"Unknown database '{alias}'. Available: {available}")
        return self._configs[alias]

    @abstractmethod
    def _get_connection(self, alias: str) -> Any:
        """Get or create database connection for the given alias."""
        ...

    @abstractmethod
    def _execute_query(
        self, alias: str, sql: str, params: dict[str, Any] | None = None
    ) -> tuple[list[str], list[tuple]]:
        """Execute SQL on the specified database and return (columns, rows)."""
        ...

    @abstractmethod
    def _get_list_tables_sql(self, schema: str | None) -> tuple[str, dict[str, Any]]:
        """Return (SQL, params) for listing tables."""
        ...

    @abstractmethod
    def _get_describe_sql(self, table_name: str, schema: str | None) -> tuple[str, dict[str, Any]]:
        """Return (SQL, params) for describing a table's columns."""
        ...

    def _format_describe_output(
        self,
        table_name: str,
        columns: list[tuple],
        extra_info: dict[str, Any] | None = None,
    ) -> str:
        """Format describe output. Subclasses can override for custom formatting."""
        lines = [f"Table: {table_name.upper()}", "", "Columns:", "-" * 80]

        for col in columns:
            # Expect at least (name, type, nullable) - subclasses may provide more
            name = col[0]
            dtype = col[1] if len(col) > 1 else "UNKNOWN"
            nullable = col[2] if len(col) > 2 else "Y"

            null_str = "NULL" if nullable in ("Y", "YES", 1, True, None) else "NOT NULL"
            lines.append(f"  {str(name):30} {str(dtype):20} {null_str}")

        if extra_info:
            if extra_info.get("primary_key"):
                lines.append("")
                lines.append(f"Primary Key: ({', '.join(extra_info['primary_key'])})")
            if extra_info.get("indexes"):
                lines.append("")
                lines.append("Indexes:")
                for idx in extra_info["indexes"]:
                    lines.append(f"  {idx}")

        return "\n".join(lines)

    def _resolve_alias(self, db_alias: str | None) -> str | None:
        """Resolve database alias, defaulting to the only config if just one exists."""
        if db_alias:
            return db_alias if db_alias in self._configs else None
        # Auto-select if only one database configured
        if len(self._configs) == 1:
            return next(iter(self._configs.keys()))
        return None

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        """Execute the database operation."""
        db_alias = self._resolve_alias(invocation.arguments.get("database"))
        operation = invocation.arguments.get("operation", "")
        row_limit = invocation.arguments.get("row_limit", self._row_limit)

        # Validate database alias
        if not db_alias:
            available = ", ".join(sorted(self._configs.keys())) or "none configured"
            provided = invocation.arguments.get("database", "")
            if provided:
                return ToolOutput(
                    content=f"Unknown database '{provided}'. Available: {available}",
                    success=False,
                )
            return ToolOutput(
                content=f"Missing required 'database' parameter. Available: {available}",
                success=False,
            )

        try:
            if operation == "query":
                return await self._handle_query(invocation, db_alias, row_limit)
            elif operation == "list_tables":
                return await self._handle_list_tables(invocation, db_alias, row_limit)
            elif operation == "describe":
                return await self._handle_describe(invocation, db_alias)
            elif operation == "export_query":
                return await self._handle_export_query(invocation, db_alias)
            else:
                return ToolOutput(
                    content=f"Unknown operation: {operation}. Use: query, list_tables, describe, export_query",
                    success=False,
                )
        except Exception as e:
            return ToolOutput(content=f"{self.db_type.title()} error: {e}", success=False)

    async def _handle_query(
        self, invocation: ToolInvocation, db_alias: str, row_limit: int
    ) -> ToolOutput:
        """Execute a SQL query (readonly mode blocks mutations)."""
        sql = invocation.arguments.get("sql", "")
        if not sql:
            return ToolOutput(content="No SQL query provided", success=False)

        # Only check for mutations in readonly mode
        if self._readonly:
            is_safe, reason = is_read_only_sql(sql)
            if not is_safe:
                return ToolOutput(content=f"Query blocked: {reason}", success=False)

        columns, rows = self._execute_query(db_alias, sql)

        if not columns:
            return ToolOutput(content="Query executed (no result set)", success=True)

        # Fetch only what we need
        rows = rows[: row_limit + 1]
        content = format_rows(columns, rows, row_limit)

        return ToolOutput(
            content=content,
            success=True,
            metadata={
                "columns": columns,
                "row_count": min(len(rows), row_limit),
                "truncated": len(rows) > row_limit,
            },
        )

    async def _handle_list_tables(
        self, invocation: ToolInvocation, db_alias: str, row_limit: int
    ) -> ToolOutput:
        """List tables matching a pattern."""
        pattern = invocation.arguments.get("table_pattern", "%")
        schema = invocation.arguments.get("schema")

        sql, params = self._get_list_tables_sql(schema)
        # Inject pattern into params
        params["pattern"] = pattern

        columns, rows = self._execute_query(db_alias, sql, params)

        if not rows:
            return ToolOutput(
                content=f"No tables found matching pattern: {pattern}",
                success=True,
            )

        rows = rows[: row_limit + 1]
        content = format_rows(columns, rows, row_limit)

        return ToolOutput(
            content=content,
            success=True,
            metadata={"table_count": min(len(rows), row_limit)},
        )

    async def _handle_describe(self, invocation: ToolInvocation, db_alias: str) -> ToolOutput:
        """Get detailed schema for a table."""
        table_name = invocation.arguments.get("table_name", "")
        schema = invocation.arguments.get("schema")

        if not table_name:
            return ToolOutput(content="No table_name provided", success=False)

        sql, params = self._get_describe_sql(table_name, schema)
        columns, rows = self._execute_query(db_alias, sql, params)

        if not rows:
            return ToolOutput(content=f"Table not found: {table_name}", success=False)

        # Get extra info (PK, indexes) if available
        extra_info = self._get_table_extra_info(db_alias, table_name, schema)

        content = self._format_describe_output(table_name, rows, extra_info)

        return ToolOutput(
            content=content,
            success=True,
            metadata={
                "table_name": table_name.upper(),
                "column_count": len(rows),
            },
        )

    async def _handle_export_query(self, invocation: ToolInvocation, db_alias: str) -> ToolOutput:
        """Export query results directly to a CSV file (streaming, memory-efficient)."""
        sql = invocation.arguments.get("sql", "")
        export_path = invocation.arguments.get("export_path", "")
        # format param reserved for future expansion (only csv supported now)

        # Validate required parameters
        if not export_path:
            return ToolOutput(content="No export_path provided", success=False)
        if not sql:
            return ToolOutput(content="No SQL query provided", success=False)

        # Resolve and validate path
        path = Path(export_path).expanduser().resolve()

        # Safety check: don't write to sensitive locations
        sensitive_patterns = [
            ".bashrc",
            ".zshrc",
            ".profile",
            ".bash_profile",
            ".ssh/",
            ".gnupg/",
            ".aws/",
            ".config/",
            "/etc/",
            "/usr/",
            "/bin/",
            "/sbin/",
        ]
        path_str_lower = str(path).lower()
        for pattern in sensitive_patterns:
            if pattern in path_str_lower:
                return ToolOutput(
                    content=f"Cannot write to sensitive location: {path}",
                    success=False,
                )

        # Fail fast: check if file exists before running query
        if path.exists():
            return ToolOutput(
                content=f"File already exists: {path}. Use a different path or delete the existing file first.",
                success=False,
            )

        # Validate SQL is read-only (only in readonly mode)
        if self._readonly:
            is_safe, reason = is_read_only_sql(sql)
            if not is_safe:
                return ToolOutput(content=f"Query blocked: {reason}", success=False)

        try:
            # Create parent directories if needed
            path.parent.mkdir(parents=True, exist_ok=True)

            # Get connection and execute query
            conn = self._get_connection(db_alias)
            cursor = conn.cursor()
            cursor.execute(sql)

            # Get column names from cursor description
            if not cursor.description:
                cursor.close()
                return ToolOutput(
                    content="Query executed but returned no result set",
                    success=False,
                )

            columns = [col[0] for col in cursor.description]

            # Stream results directly to file
            row_count = 0
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(columns)

                for row in cursor:
                    writer.writerow(row)
                    row_count += 1

            cursor.close()

            # Get final file size
            file_size = path.stat().st_size

            return ToolOutput(
                content=f"Exported {row_count} rows ({len(columns)} columns) to {path} ({file_size} bytes)",
                success=True,
                metadata={
                    "path": str(path),
                    "row_count": row_count,
                    "column_count": len(columns),
                    "columns": columns,
                    "size_bytes": file_size,
                },
            )

        except PermissionError:
            return ToolOutput(content=f"Permission denied: {path}", success=False)
        except Exception as e:
            # Clean up partial file on error
            if path.exists():
                path.unlink()
            return ToolOutput(
                content=f"{self.db_type.title()} export error: {e}",
                success=False,
            )

    def _get_table_extra_info(
        self, alias: str, table_name: str, schema: str | None
    ) -> dict[str, Any] | None:
        """Get additional table info (PK, indexes). Override in subclasses."""
        return None
