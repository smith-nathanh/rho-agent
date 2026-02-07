"""PostgreSQL database handler."""

from __future__ import annotations

from typing import Any

from ...config.databases import DatabaseConfig
from .database import DatabaseHandler

# Check for psycopg availability (prefer psycopg3, fallback to psycopg2)
try:
    import psycopg

    PSYCOPG_VERSION = 3
except ImportError:
    try:
        import psycopg2 as psycopg  # type: ignore[import-not-found]

        PSYCOPG_VERSION = 2
    except ImportError:
        psycopg = None  # type: ignore[assignment]
        PSYCOPG_VERSION = 0

# System schemas to filter out by default
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


class PostgresHandler(DatabaseHandler):
    """PostgreSQL database handler with configurable readonly mode.

    Supports multiple databases via the configs parameter. Each database
    is identified by an alias and must specify a `database` parameter
    in tool calls (unless only one database is configured).
    """

    def __init__(
        self,
        configs: list[DatabaseConfig],
        **kwargs: Any,
    ) -> None:
        """Initialize PostgreSQL handler.

        Args:
            configs: List of database configurations.
            **kwargs: Passed to DatabaseHandler (row_limit, readonly, etc.)
        """
        super().__init__(configs=configs, **kwargs)

    @property
    def db_type(self) -> str:
        return "postgres"

    def _get_connection(self, alias: str) -> Any:
        """Get or create connection for the specified database alias."""
        if psycopg is None:
            raise RuntimeError("PostgreSQL driver not available. Install psycopg: uv add psycopg")

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
            raise RuntimeError(f"No database name configured for PostgreSQL '{alias}'")

        if PSYCOPG_VERSION == 3:
            # psycopg3 connection
            conn = psycopg.connect(
                host=config.host or "localhost",
                port=config.port or 5432,
                dbname=config.database,
                user=config.user or "",
                password=config.password or "",
                autocommit=True,
            )
            # Set session to read-only when readonly mode is enabled
            if self._readonly:
                with conn.cursor() as cur:
                    cur.execute("SET default_transaction_read_only = ON")
        else:
            # psycopg2 connection
            conn = psycopg.connect(
                host=config.host or "localhost",
                port=config.port or 5432,
                database=config.database,
                user=config.user or "",
                password=config.password or "",
            )
            conn.set_session(readonly=self._readonly, autocommit=True)

        self._connections[alias] = conn
        return conn

    def _execute_query(
        self, alias: str, sql: str, params: dict[str, Any] | None = None
    ) -> tuple[list[str], list[tuple]]:
        conn = self._get_connection(alias)
        cursor = conn.cursor()

        # PostgreSQL uses %(name)s for named params
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)

        if cursor.description is None:
            return [], []

        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()
        cursor.close()
        return columns, rows

    def _get_list_tables_sql(self, schema: str | None) -> tuple[str, dict[str, Any]]:
        if schema:
            # Filter by specific schema
            return (
                """
                SELECT table_schema, table_name, table_type
                FROM information_schema.tables
                WHERE table_schema = %(schema)s
                  AND table_name LIKE %(pattern)s
                ORDER BY table_schema, table_name
                """,
                {"schema": schema},
            )
        else:
            # Exclude system schemas
            return (
                """
                SELECT table_schema, table_name, table_type
                FROM information_schema.tables
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
                  AND table_name LIKE %(pattern)s
                ORDER BY table_schema, table_name
                """,
                {},
            )

    def _get_describe_sql(self, table_name: str, schema: str | None) -> tuple[str, dict[str, Any]]:
        if schema:
            return (
                """
                SELECT
                    column_name,
                    CASE
                        WHEN character_maximum_length IS NOT NULL
                            THEN data_type || '(' || character_maximum_length || ')'
                        WHEN numeric_precision IS NOT NULL AND numeric_scale IS NOT NULL
                            THEN data_type || '(' || numeric_precision || ',' || numeric_scale || ')'
                        WHEN numeric_precision IS NOT NULL
                            THEN data_type || '(' || numeric_precision || ')'
                        ELSE data_type
                    END as data_type,
                    is_nullable
                FROM information_schema.columns
                WHERE table_schema = %(schema)s
                  AND table_name = %(table_name)s
                ORDER BY ordinal_position
                """,
                {"schema": schema, "table_name": table_name},
            )
        else:
            # Search in non-system schemas
            return (
                """
                SELECT
                    column_name,
                    CASE
                        WHEN character_maximum_length IS NOT NULL
                            THEN data_type || '(' || character_maximum_length || ')'
                        WHEN numeric_precision IS NOT NULL AND numeric_scale IS NOT NULL
                            THEN data_type || '(' || numeric_precision || ',' || numeric_scale || ')'
                        WHEN numeric_precision IS NOT NULL
                            THEN data_type || '(' || numeric_precision || ')'
                        ELSE data_type
                    END as data_type,
                    is_nullable
                FROM information_schema.columns
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
                  AND table_name = %(table_name)s
                ORDER BY ordinal_position
                """,
                {"table_name": table_name},
            )

    def _get_table_extra_info(
        self, alias: str, table_name: str, schema: str | None
    ) -> dict[str, Any] | None:
        conn = self._get_connection(alias)
        cursor = conn.cursor()
        extra: dict[str, Any] = {}

        # Build schema condition
        if schema:
            schema_condition = "n.nspname = %s"
            schema_params: tuple[Any, ...] = (schema, table_name)
        else:
            schema_condition = "n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
            schema_params = (table_name,)

        # Primary key columns
        pk_sql = f"""
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE i.indisprimary
              AND {schema_condition}
              AND c.relname = %s
            ORDER BY array_position(i.indkey, a.attnum)
        """
        cursor.execute(pk_sql, schema_params)
        pk_cols = [row[0] for row in cursor.fetchall()]
        if pk_cols:
            extra["primary_key"] = pk_cols

        # Indexes
        if schema:
            idx_sql = """
                SELECT indexname || ' (' ||
                    CASE WHEN indexdef LIKE '%UNIQUE%' THEN 'UNIQUE' ELSE 'NONUNIQUE' END
                    || ')'
                FROM pg_indexes
                WHERE schemaname = %s AND tablename = %s
            """
            cursor.execute(idx_sql, (schema, table_name))
        else:
            idx_sql = """
                SELECT indexname || ' (' ||
                    CASE WHEN indexdef LIKE '%UNIQUE%' THEN 'UNIQUE' ELSE 'NONUNIQUE' END
                    || ')'
                FROM pg_indexes
                WHERE schemaname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
                  AND tablename = %s
            """
            cursor.execute(idx_sql, (table_name,))

        indexes = [row[0] for row in cursor.fetchall()]
        if indexes:
            extra["indexes"] = indexes

        cursor.close()
        return extra if extra else None
