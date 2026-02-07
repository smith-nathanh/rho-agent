"""Vertica database handler."""

from __future__ import annotations

from typing import Any

from ...config.databases import DatabaseConfig
from .database import DatabaseHandler

try:
    import vertica_python

    VERTICA_AVAILABLE = True
except ImportError:
    VERTICA_AVAILABLE = False


class VerticaHandler(DatabaseHandler):
    """Vertica database handler with configurable readonly mode.

    Supports multiple databases via the configs parameter. Each database
    is identified by an alias and must specify a `database` parameter
    in tool calls (unless only one database is configured).
    """

    def __init__(
        self,
        configs: list[DatabaseConfig],
        **kwargs: Any,
    ) -> None:
        """Initialize Vertica handler.

        Args:
            configs: List of database configurations.
            **kwargs: Passed to DatabaseHandler (row_limit, readonly, etc.)
        """
        super().__init__(configs=configs, **kwargs)

    @property
    def db_type(self) -> str:
        return "vertica"

    def _get_connection(self, alias: str) -> Any:
        """Get or create connection for the specified database alias."""
        if not VERTICA_AVAILABLE:
            raise RuntimeError("vertica-python package not installed. Run: uv add vertica-python")

        # Check if existing connection is still valid
        if alias in self._connections:
            conn = self._connections[alias]
            try:
                # Test connection with a simple query
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                return conn
            except Exception:
                # Connection lost, remove from cache
                try:
                    conn.close()
                except Exception:
                    pass
                del self._connections[alias]

        config = self._get_config(alias)
        if not config.database:
            raise RuntimeError(f"No database name configured for Vertica '{alias}'")

        conn = vertica_python.connect(
            host=config.host or "localhost",
            port=config.port or 5433,
            database=config.database,
            user=config.user or "",
            password=config.password or "",
            read_only=self._readonly,  # Honor readonly mode
        )
        self._connections[alias] = conn
        return conn

    def _execute_query(
        self, alias: str, sql: str, params: dict[str, Any] | None = None
    ) -> tuple[list[str], list[tuple]]:
        conn = self._get_connection(alias)
        with conn.cursor() as cursor:
            cursor.execute(sql, params or {})
            if cursor.description is None:
                return [], []
            columns = [col.name for col in cursor.description]
            rows = cursor.fetchall()
            return columns, rows

    def _get_list_tables_sql(self, schema: str | None) -> tuple[str, dict[str, Any]]:
        if schema:
            return (
                """
                SELECT table_schema, table_name,
                       CASE WHEN is_temp_table THEN 'TEMP' ELSE 'TABLE' END as table_type
                FROM v_catalog.tables
                WHERE table_schema = :schema
                  AND table_name ILIKE :pattern
                ORDER BY table_schema, table_name
                """,
                {"schema": schema},
            )
        return (
            """
            SELECT table_schema, table_name,
                   CASE WHEN is_temp_table THEN 'TEMP' ELSE 'TABLE' END as table_type
            FROM v_catalog.tables
            WHERE table_schema NOT IN ('v_catalog', 'v_monitor', 'v_internal')
              AND table_name ILIKE :pattern
            ORDER BY table_schema, table_name
            """,
            {},
        )

    def _get_describe_sql(self, table_name: str, schema: str | None) -> tuple[str, dict[str, Any]]:
        if schema:
            return (
                """
                SELECT column_name,
                       data_type || CASE
                           WHEN character_maximum_length IS NOT NULL
                               THEN '(' || character_maximum_length || ')'
                           WHEN numeric_precision IS NOT NULL
                               THEN '(' || numeric_precision ||
                                   CASE WHEN numeric_scale IS NOT NULL
                                       THEN ',' || numeric_scale ELSE '' END || ')'
                           ELSE ''
                       END as data_type,
                       CASE WHEN is_nullable THEN 'Y' ELSE 'N' END as nullable
                FROM v_catalog.columns
                WHERE table_schema = :schema
                  AND table_name = :table_name
                ORDER BY ordinal_position
                """,
                {"schema": schema, "table_name": table_name},
            )
        # Without schema, search all user schemas
        return (
            """
            SELECT column_name,
                   data_type || CASE
                       WHEN character_maximum_length IS NOT NULL
                           THEN '(' || character_maximum_length || ')'
                       WHEN numeric_precision IS NOT NULL
                           THEN '(' || numeric_precision ||
                               CASE WHEN numeric_scale IS NOT NULL
                                   THEN ',' || numeric_scale ELSE '' END || ')'
                       ELSE ''
                   END as data_type,
                   CASE WHEN is_nullable THEN 'Y' ELSE 'N' END as nullable
            FROM v_catalog.columns
            WHERE table_schema NOT IN ('v_catalog', 'v_monitor', 'v_internal')
              AND table_name = :table_name
            ORDER BY ordinal_position
            """,
            {"table_name": table_name},
        )

    def _get_table_extra_info(
        self, alias: str, table_name: str, schema: str | None
    ) -> dict[str, Any] | None:
        conn = self._get_connection(alias)
        extra: dict[str, Any] = {}

        with conn.cursor() as cursor:
            # Primary key
            if schema:
                pk_sql = """
                    SELECT column_name
                    FROM v_catalog.primary_keys
                    WHERE table_schema = :schema
                      AND table_name = :table_name
                    ORDER BY ordinal_position
                """
                cursor.execute(pk_sql, {"schema": schema, "table_name": table_name})
            else:
                pk_sql = """
                    SELECT column_name
                    FROM v_catalog.primary_keys
                    WHERE table_schema NOT IN ('v_catalog', 'v_monitor', 'v_internal')
                      AND table_name = :table_name
                    ORDER BY ordinal_position
                """
                cursor.execute(pk_sql, {"table_name": table_name})

            pk_cols = [row[0] for row in cursor.fetchall()]
            if pk_cols:
                extra["primary_key"] = pk_cols

            # Projections (Vertica's equivalent of indexes/materialized views)
            if schema:
                proj_sql = """
                    SELECT projection_name || ' (' ||
                           CASE WHEN is_super_projection THEN 'SUPER' ELSE 'STANDARD' END || ')'
                    FROM v_catalog.projections
                    WHERE anchor_table_schema = :schema
                      AND anchor_table_name = :table_name
                """
                cursor.execute(proj_sql, {"schema": schema, "table_name": table_name})
            else:
                proj_sql = """
                    SELECT projection_name || ' (' ||
                           CASE WHEN is_super_projection THEN 'SUPER' ELSE 'STANDARD' END || ')'
                    FROM v_catalog.projections
                    WHERE anchor_table_schema NOT IN ('v_catalog', 'v_monitor', 'v_internal')
                      AND anchor_table_name = :table_name
                """
                cursor.execute(proj_sql, {"table_name": table_name})

            projections = [row[0] for row in cursor.fetchall()]
            if projections:
                extra["indexes"] = projections  # Reuse indexes field for projections

        return extra if extra else None
