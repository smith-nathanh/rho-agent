"""MySQL database handler."""

from __future__ import annotations

from typing import Any

from ...config.databases import DatabaseConfig
from .database import DatabaseHandler

# Check for mysql-connector-python availability
try:
    import mysql.connector
    from mysql.connector import Error as MySQLError

    MYSQL_AVAILABLE = True
except ImportError:
    mysql = None  # type: ignore[assignment]
    MYSQL_AVAILABLE = False

# System schemas to filter out by default
SYSTEM_SCHEMAS = ("mysql", "information_schema", "performance_schema", "sys")


class MysqlHandler(DatabaseHandler):
    """MySQL database handler with configurable readonly mode.

    Supports multiple databases via the configs parameter. Each database
    is identified by an alias and must specify a `database` parameter
    in tool calls (unless only one database is configured).
    """

    def __init__(
        self,
        configs: list[DatabaseConfig],
        **kwargs: Any,
    ) -> None:
        """Initialize MySQL handler.

        Args:
            configs: List of database configurations.
            **kwargs: Passed to DatabaseHandler (row_limit, readonly, etc.)
        """
        super().__init__(configs=configs, **kwargs)

    @property
    def db_type(self) -> str:
        return "mysql"


    def _get_connection(self, alias: str) -> Any:
        """Get or create connection for the specified database alias."""
        if not MYSQL_AVAILABLE:
            raise RuntimeError(
                "MySQL driver not available. Install mysql-connector-python: uv add mysql-connector-python"
            )

        # Check if existing connection is still valid
        if alias in self._connections:
            conn = self._connections[alias]
            if conn.is_connected():
                return conn
            # Connection lost, remove from cache
            del self._connections[alias]

        config = self._get_config(alias)
        if not config.database:
            raise RuntimeError(
                f"No database name configured for MySQL '{alias}'"
            )

        conn = mysql.connector.connect(
            host=config.host or "localhost",
            port=config.port or 3306,
            database=config.database,
            user=config.user or "",
            password=config.password or "",
            autocommit=True,
        )
        # Set session to read-only mode when readonly is enabled
        if self._readonly:
            cursor = conn.cursor()
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.close()

        self._connections[alias] = conn
        return conn

    def _execute_query(
        self, alias: str, sql: str, params: dict[str, Any] | None = None
    ) -> tuple[list[str], list[tuple]]:
        conn = self._get_connection(alias)
        cursor = conn.cursor()

        # MySQL connector uses %(name)s for named params
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)

        if cursor.description is None:
            cursor.close()
            return [], []

        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()
        cursor.close()
        return columns, rows

    def _get_list_tables_sql(self, schema: str | None) -> tuple[str, dict[str, Any]]:
        if schema:
            # Filter by specific schema/database
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
            # Use current database, exclude system schemas
            return (
                """
                SELECT table_schema, table_name, table_type
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
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
                            THEN CONCAT(data_type, '(', character_maximum_length, ')')
                        WHEN numeric_precision IS NOT NULL AND numeric_scale IS NOT NULL AND numeric_scale > 0
                            THEN CONCAT(data_type, '(', numeric_precision, ',', numeric_scale, ')')
                        WHEN numeric_precision IS NOT NULL
                            THEN CONCAT(data_type, '(', numeric_precision, ')')
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
            # Use current database
            return (
                """
                SELECT
                    column_name,
                    CASE
                        WHEN character_maximum_length IS NOT NULL
                            THEN CONCAT(data_type, '(', character_maximum_length, ')')
                        WHEN numeric_precision IS NOT NULL AND numeric_scale IS NOT NULL AND numeric_scale > 0
                            THEN CONCAT(data_type, '(', numeric_precision, ',', numeric_scale, ')')
                        WHEN numeric_precision IS NOT NULL
                            THEN CONCAT(data_type, '(', numeric_precision, ')')
                        ELSE data_type
                    END as data_type,
                    is_nullable
                FROM information_schema.columns
                WHERE table_schema = DATABASE()
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
            schema_val = schema
        else:
            # Get current database name
            cursor.execute("SELECT DATABASE()")
            result = cursor.fetchone()
            schema_val = result[0] if result else None
            if not schema_val:
                cursor.close()
                return None

        # Primary key columns
        pk_sql = """
            SELECT column_name
            FROM information_schema.key_column_usage
            WHERE table_schema = %s
              AND table_name = %s
              AND constraint_name = 'PRIMARY'
            ORDER BY ordinal_position
        """
        cursor.execute(pk_sql, (schema_val, table_name))
        pk_cols = [row[0] for row in cursor.fetchall()]
        if pk_cols:
            extra["primary_key"] = pk_cols

        # Indexes
        idx_sql = """
            SELECT CONCAT(
                index_name,
                ' (',
                CASE WHEN non_unique = 0 THEN 'UNIQUE' ELSE 'NONUNIQUE' END,
                ')'
            )
            FROM information_schema.statistics
            WHERE table_schema = %s
              AND table_name = %s
            GROUP BY index_name, non_unique
            ORDER BY index_name
        """
        cursor.execute(idx_sql, (schema_val, table_name))
        indexes = [row[0] for row in cursor.fetchall()]
        if indexes:
            extra["indexes"] = indexes

        cursor.close()
        return extra if extra else None
