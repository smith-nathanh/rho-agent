"""SQLite database handler."""

from __future__ import annotations

import sqlite3
from typing import Any

from .database_config import DatabaseConfig
from .database import DatabaseHandler


def _quote_pragma_arg(name: str) -> str:
    """Quote a table name for use in SQLite PRAGMA functions.

    PRAGMA functions expect single-quoted string literals for names with
    special characters. This is safe because PRAGMA doesn't execute SQL -
    it just looks up metadata for the literal table name.
    """
    # Escape single quotes by doubling them, wrap in quotes
    return f"'{name.replace(chr(39), chr(39) + chr(39))}'"


class SqliteHandler(DatabaseHandler):
    """SQLite database handler with configurable readonly mode.

    Supports multiple databases via the configs parameter. Each database
    is identified by an alias and must specify a `database` parameter
    in tool calls (unless only one database is configured).
    """

    def __init__(
        self,
        configs: list[DatabaseConfig],
        **kwargs: Any,
    ) -> None:
        """Initialize SQLite handler.

        Args:
            configs: List of database configurations.
            **kwargs: Passed to DatabaseHandler (row_limit, readonly, etc.)
        """
        super().__init__(configs=configs, **kwargs)

    @property
    def db_type(self) -> str:
        return "sqlite"

    def _get_connection(self, alias: str) -> sqlite3.Connection:
        """Get or create connection for the specified database alias."""
        # Check if existing connection is still valid
        if alias in self._connections:
            conn = self._connections[alias]
            try:
                # Test connection with a simple query
                conn.execute("SELECT 1")
                return conn
            except Exception:
                # Connection lost, remove from cache
                try:
                    conn.close()
                except Exception:
                    pass
                del self._connections[alias]

        config = self._get_config(alias)
        if not config.path:
            raise RuntimeError(f"No path configured for SQLite database '{alias}'")

        # Open in read-only or read-write mode based on readonly flag
        mode = "ro" if self._readonly else "rw"
        conn = sqlite3.connect(
            f"file:{config.path}?mode={mode}",
            uri=True,
            check_same_thread=False,
        )
        self._connections[alias] = conn
        return conn

    def _execute_query(
        self, alias: str, sql: str, params: dict[str, Any] | None = None
    ) -> tuple[list[str], list[tuple]]:
        conn = self._get_connection(alias)
        cursor = conn.cursor()

        # SQLite uses ? or :name for params
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)

        if cursor.description is None:
            return [], []

        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()
        return columns, rows

    def _get_list_tables_sql(self, schema: str | None) -> tuple[str, dict[str, Any]]:
        # schema param is ignored for SQLite (single schema per file)
        return (
            """
            SELECT name AS table_name, type
            FROM sqlite_master
            WHERE type IN ('table', 'view')
              AND name NOT LIKE 'sqlite_%'
              AND name LIKE :pattern
            ORDER BY type, name
            """,
            {},
        )

    def _get_describe_sql(self, table_name: str, schema: str | None) -> tuple[str, dict[str, Any]]:
        # SQLite's PRAGMA doesn't support parameterized table names,
        # so we quote the identifier for safety
        safe_name = _quote_pragma_arg(table_name)
        return (
            f"""
            SELECT name, type,
                CASE WHEN "notnull" = 1 THEN 'N' ELSE 'Y' END as nullable
            FROM pragma_table_info({safe_name})
            ORDER BY cid
            """,
            {},
        )

    def _get_table_extra_info(
        self, alias: str, table_name: str, schema: str | None
    ) -> dict[str, Any] | None:
        conn = self._get_connection(alias)
        cursor = conn.cursor()
        extra: dict[str, Any] = {}

        safe_name = _quote_pragma_arg(table_name)

        # Primary key columns
        cursor.execute(f"SELECT name FROM pragma_table_info({safe_name}) WHERE pk > 0 ORDER BY pk")
        pk_cols = [row[0] for row in cursor.fetchall()]
        if pk_cols:
            extra["primary_key"] = pk_cols

        # Indexes
        cursor.execute(
            f"SELECT name || ' (' || CASE WHEN \"unique\" THEN 'UNIQUE' ELSE 'NONUNIQUE' END || ')' FROM pragma_index_list({safe_name})"
        )
        indexes = [row[0] for row in cursor.fetchall()]
        if indexes:
            extra["indexes"] = indexes

        return extra if extra else None
