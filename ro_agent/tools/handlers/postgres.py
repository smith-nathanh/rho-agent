"""PostgreSQL database handler."""

import os
from typing import Any

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
    """Read-only PostgreSQL database handler."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        database: str | None = None,
        user: str | None = None,
        password: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize PostgreSQL handler.

        Connection can be configured via constructor args or environment variables:
        - POSTGRES_HOST: Database host (default: localhost)
        - POSTGRES_PORT: Database port (default: 5432)
        - POSTGRES_DATABASE: Database name
        - POSTGRES_USER: Username
        - POSTGRES_PASSWORD: Password
        """
        super().__init__(**kwargs)
        self._host = host or os.environ.get("POSTGRES_HOST", "localhost")
        self._port = port or int(os.environ.get("POSTGRES_PORT", "5432"))
        self._database = database or os.environ.get("POSTGRES_DATABASE", "")
        self._user = user or os.environ.get("POSTGRES_USER", "")
        self._password = password or os.environ.get("POSTGRES_PASSWORD", "")
        self._connection: Any = None

    @property
    def db_type(self) -> str:
        return "postgres"

    @property
    def description(self) -> str:
        db_info = f"{self._database}@{self._host}" if self._database else "PostgreSQL"
        return (
            f"Query the PostgreSQL database ({db_info}). "
            f"Use 'list_tables' to see available tables, 'describe' for table schema, "
            f"'query' for SELECT queries, 'export_query' to export results to CSV. "
            f"All operations are read-only."
        )

    def _get_connection(self) -> Any:
        if psycopg is None:
            raise RuntimeError(
                "PostgreSQL driver not available. Install psycopg: uv add psycopg"
            )

        if not self._database:
            raise RuntimeError(
                "No PostgreSQL database configured. Set POSTGRES_DATABASE env var."
            )

        if self._connection is None:
            if PSYCOPG_VERSION == 3:
                # psycopg3 connection
                self._connection = psycopg.connect(
                    host=self._host,
                    port=self._port,
                    dbname=self._database,
                    user=self._user,
                    password=self._password,
                    autocommit=True,  # Read-only, no transactions needed
                )
                # Set session to read-only for extra safety
                with self._connection.cursor() as cur:
                    cur.execute("SET default_transaction_read_only = ON")
            else:
                # psycopg2 connection
                self._connection = psycopg.connect(
                    host=self._host,
                    port=self._port,
                    database=self._database,
                    user=self._user,
                    password=self._password,
                )
                self._connection.set_session(readonly=True, autocommit=True)

        return self._connection

    def _execute_query(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> tuple[list[str], list[tuple]]:
        conn = self._get_connection()
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

    def _get_describe_sql(
        self, table_name: str, schema: str | None
    ) -> tuple[str, dict[str, Any]]:
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
        self, table_name: str, schema: str | None
    ) -> dict[str, Any] | None:
        conn = self._get_connection()
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
