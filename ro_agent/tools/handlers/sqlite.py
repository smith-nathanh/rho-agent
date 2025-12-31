"""SQLite database handler."""

import os
import sqlite3
from typing import Any

from .database import DatabaseHandler


class SqliteHandler(DatabaseHandler):
    """Read-only SQLite database handler."""

    def __init__(self, db_path: str | None = None, **kwargs: Any) -> None:
        """Initialize SQLite handler.

        Connection can be configured via constructor arg or environment variable:
        - SQLITE_DB: Path to SQLite database file
        """
        super().__init__(**kwargs)
        self._db_path = db_path or os.environ.get("SQLITE_DB", "")
        self._connection: sqlite3.Connection | None = None

    @property
    def db_type(self) -> str:
        return "sqlite"

    def _get_connection(self) -> sqlite3.Connection:
        if not self._db_path:
            raise RuntimeError("No SQLite database configured. Set SQLITE_DB env var.")

        if self._connection is None:
            # Open in read-only mode via URI
            self._connection = sqlite3.connect(
                f"file:{self._db_path}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
        return self._connection

    def _execute_query(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> tuple[list[str], list[tuple]]:
        conn = self._get_connection()
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

    def _get_describe_sql(
        self, table_name: str, schema: str | None
    ) -> tuple[str, dict[str, Any]]:
        # SQLite's PRAGMA doesn't support parameterized table names,
        # so we validate and use string formatting
        # Table name is validated by the caller
        safe_name = table_name.replace("'", "''").replace('"', '""')
        return (
            f"""
            SELECT name, type,
                CASE WHEN "notnull" = 1 THEN 'N' ELSE 'Y' END as nullable
            FROM pragma_table_info('{safe_name}')
            ORDER BY cid
            """,
            {},
        )

    def _get_table_extra_info(
        self, table_name: str, schema: str | None
    ) -> dict[str, Any] | None:
        conn = self._get_connection()
        cursor = conn.cursor()
        extra: dict[str, Any] = {}

        safe_name = table_name.replace("'", "''").replace('"', '""')

        # Primary key columns
        cursor.execute(f"SELECT name FROM pragma_table_info('{safe_name}') WHERE pk > 0 ORDER BY pk")
        pk_cols = [row[0] for row in cursor.fetchall()]
        if pk_cols:
            extra["primary_key"] = pk_cols

        # Indexes
        cursor.execute(f"SELECT name || ' (' || CASE WHEN \"unique\" THEN 'UNIQUE' ELSE 'NONUNIQUE' END || ')' FROM pragma_index_list('{safe_name}')")
        indexes = [row[0] for row in cursor.fetchall()]
        if indexes:
            extra["indexes"] = indexes

        return extra if extra else None
