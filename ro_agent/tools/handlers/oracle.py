"""Oracle database handler."""

import os
from typing import Any

from .database import DatabaseHandler

try:
    import oracledb

    ORACLEDB_AVAILABLE = True
except ImportError:
    ORACLEDB_AVAILABLE = False


class OracleHandler(DatabaseHandler):
    """Read-only Oracle database handler."""

    def __init__(
        self,
        dsn: str | None = None,
        user: str | None = None,
        password: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._dsn = dsn or os.environ.get("ORACLE_DSN", "")
        self._user = user or os.environ.get("ORACLE_USER", "")
        self._password = password or os.environ.get("ORACLE_PASSWORD", "")
        self._connection: Any = None

    @property
    def db_type(self) -> str:
        return "oracle"

    def _get_connection(self) -> Any:
        if not ORACLEDB_AVAILABLE:
            raise RuntimeError("oracledb package not installed. Run: uv add oracledb")

        if self._connection is None:
            self._connection = oracledb.connect(
                user=self._user,
                password=self._password,
                dsn=self._dsn,
            )
        return self._connection

    def _execute_query(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> tuple[list[str], list[tuple]]:
        conn = self._get_connection()
        with conn.cursor() as cursor:
            cursor.execute(sql, params or {})
            if cursor.description is None:
                return [], []
            columns = [col[0] for col in cursor.description]
            rows = cursor.fetchall()
            return columns, rows

    def _get_list_tables_sql(self, schema: str | None) -> tuple[str, dict[str, Any]]:
        if schema:
            return (
                """
                SELECT owner, table_name, num_rows, last_analyzed
                FROM all_tables
                WHERE owner = UPPER(:schema)
                  AND table_name LIKE UPPER(:pattern)
                ORDER BY owner, table_name
                """,
                {"schema": schema},
            )
        return (
            """
            SELECT table_name, num_rows, last_analyzed
            FROM user_tables
            WHERE table_name LIKE UPPER(:pattern)
            ORDER BY table_name
            """,
            {},
        )

    def _get_describe_sql(
        self, table_name: str, schema: str | None
    ) -> tuple[str, dict[str, Any]]:
        if schema:
            return (
                """
                SELECT column_name, data_type ||
                    CASE
                        WHEN data_precision IS NOT NULL THEN '(' || data_precision ||
                            CASE WHEN data_scale IS NOT NULL THEN ',' || data_scale ELSE '' END || ')'
                        WHEN data_type IN ('VARCHAR2','CHAR','RAW') THEN '(' || data_length || ')'
                        ELSE ''
                    END AS data_type,
                    nullable
                FROM all_tab_columns
                WHERE owner = UPPER(:schema)
                  AND table_name = UPPER(:table_name)
                ORDER BY column_id
                """,
                {"schema": schema, "table_name": table_name},
            )
        return (
            """
            SELECT column_name, data_type ||
                CASE
                    WHEN data_precision IS NOT NULL THEN '(' || data_precision ||
                        CASE WHEN data_scale IS NOT NULL THEN ',' || data_scale ELSE '' END || ')'
                    WHEN data_type IN ('VARCHAR2','CHAR','RAW') THEN '(' || data_length || ')'
                    ELSE ''
                END AS data_type,
                nullable
            FROM user_tab_columns
            WHERE table_name = UPPER(:table_name)
            ORDER BY column_id
            """,
            {"table_name": table_name},
        )

    def _get_table_extra_info(
        self, table_name: str, schema: str | None
    ) -> dict[str, Any] | None:
        conn = self._get_connection()
        extra: dict[str, Any] = {}

        with conn.cursor() as cursor:
            # Primary key
            if schema:
                pk_sql = """
                    SELECT cols.column_name
                    FROM all_constraints cons
                    JOIN all_cons_columns cols
                        ON cons.constraint_name = cols.constraint_name
                        AND cons.owner = cols.owner
                    WHERE cons.constraint_type = 'P'
                      AND cons.owner = UPPER(:schema)
                      AND cons.table_name = UPPER(:table_name)
                    ORDER BY cols.position
                """
                cursor.execute(pk_sql, {"schema": schema, "table_name": table_name})
            else:
                pk_sql = """
                    SELECT cols.column_name
                    FROM user_constraints cons
                    JOIN user_cons_columns cols
                        ON cons.constraint_name = cols.constraint_name
                    WHERE cons.constraint_type = 'P'
                      AND cons.table_name = UPPER(:table_name)
                    ORDER BY cols.position
                """
                cursor.execute(pk_sql, {"table_name": table_name})

            pk_cols = [row[0] for row in cursor.fetchall()]
            if pk_cols:
                extra["primary_key"] = pk_cols

            # Indexes
            if schema:
                idx_sql = """
                    SELECT index_name || ' (' || uniqueness || ')'
                    FROM all_indexes
                    WHERE owner = UPPER(:schema)
                      AND table_name = UPPER(:table_name)
                """
                cursor.execute(idx_sql, {"schema": schema, "table_name": table_name})
            else:
                idx_sql = """
                    SELECT index_name || ' (' || uniqueness || ')'
                    FROM user_indexes
                    WHERE table_name = UPPER(:table_name)
                """
                cursor.execute(idx_sql, {"table_name": table_name})

            indexes = [row[0] for row in cursor.fetchall()]
            if indexes:
                extra["indexes"] = indexes

        return extra if extra else None
