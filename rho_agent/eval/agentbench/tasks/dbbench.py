"""DBBench task loader and data structures."""

import json
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import BaseTask


@dataclass
class TableInfo:
    """Information about a database table."""

    columns: list[dict[str, str]]  # [{"name": str, "type": str}, ...]
    rows: list[list[Any]]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for hash calculation."""
        return {"columns": self.columns, "rows": self.rows}


@dataclass
class DBBenchTask(BaseTask):
    """A DBBench evaluation task."""

    table_info: TableInfo
    table_name: str
    expected_answer: list[str]  # "label" field in the data
    query_type: str  # SELECT, INSERT, UPDATE, DELETE (inferred from type field)
    ground_truth_sql: str | None = None
    add_description: str = ""  # Additional table description
    source: str = ""  # wikisql, wikitq, etc.
    answer_md5: str | None = None  # Pre-computed hash for mutation queries

    def get_prompt(self) -> str:
        """Get the prompt to send to the agent.

        Includes the task description and table context.
        """
        # Build table context
        column_names = [col["name"] for col in self.table_info.columns]
        column_types = [col["type"] for col in self.table_info.columns]

        # Format column info
        col_info = ", ".join(f"{name} ({dtype})" for name, dtype in zip(column_names, column_types))

        # Sample rows (first 3)
        sample_rows = self.table_info.rows[:3]
        rows_str = "\n".join(str(row) for row in sample_rows)

        prompt = f"""{self.description}

Table: {self.table_name}
Columns: {col_info}

Sample rows:
{rows_str}

{self.add_description}

Use execute_sql to query the database. When you have the answer, use commit_final_answer to submit it."""

        return prompt

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "index": self.index,
            "description": self.description,
            "table_name": self.table_name,
            "expected_answer": self.expected_answer,
            "query_type": self.query_type,
            "ground_truth_sql": self.ground_truth_sql,
        }


def infer_query_type(sql: str | None, types: list[str] | None) -> str:
    """Infer the query type from SQL or type field."""
    # Most DBBench tasks are SELECT queries
    if types and len(types) > 0:
        t = types[0].upper()
        if t in ("INSERT", "UPDATE", "DELETE"):
            return t

    if sql:
        sql_upper = sql.upper().strip()
        if sql_upper.startswith("INSERT"):
            return "INSERT"
        elif sql_upper.startswith("UPDATE"):
            return "UPDATE"
        elif sql_upper.startswith("DELETE"):
            return "DELETE"

    return "SELECT"


def load_dbbench_tasks(path: str | Path) -> list[DBBenchTask]:
    """Load DBBench tasks from a JSONL file.

    Args:
        path: Path to the standard.jsonl file

    Returns:
        List of DBBenchTask objects
    """
    path = Path(path)
    tasks = []

    with open(path, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            line = line.strip()
            if not line:
                continue

            data = json.loads(line)

            # Extract table info
            table_data = data.get("table", {})
            table_info_data = table_data.get("table_info", {})

            table_info = TableInfo(
                columns=table_info_data.get("columns", []),
                rows=table_info_data.get("rows", []),
            )

            # Extract SQL info
            sql_data = data.get("sql", {})
            sql_query = sql_data.get("query") if isinstance(sql_data, dict) else None

            # Infer query type
            query_type = infer_query_type(sql_query, data.get("type"))

            task = DBBenchTask(
                index=idx,
                description=data.get("description", ""),
                table_info=table_info,
                table_name=table_data.get("table_name", "data"),
                expected_answer=data.get("label", []),
                query_type=query_type,
                ground_truth_sql=sql_query,
                add_description=data.get("add_description", ""),
                source=data.get("source", ""),
                answer_md5=data.get("answer_md5"),
            )
            tasks.append(task)

    return tasks


def create_sqlite_from_tableinfo(
    table_name: str, table_info: TableInfo, db_path: str | Path | None = None
) -> Path:
    """Create a SQLite database from table info.

    Args:
        table_name: Name for the table
        table_info: TableInfo with columns and rows
        db_path: Optional path for the database file. If None, creates a temp file.

    Returns:
        Path to the created database file
    """
    if db_path is None:
        # Create a temp file
        fd, db_path = tempfile.mkstemp(suffix=".db")
        import os

        os.close(fd)

    db_path = Path(db_path)

    # Create the database
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Build CREATE TABLE statement
    # Quote table name to handle special characters/spaces
    safe_table_name = f'"{table_name}"'

    # Build column definitions
    col_defs = []
    for col in table_info.columns:
        col_name = f'"{col["name"]}"'
        col_type = col.get("type", "TEXT").upper()
        # Map common types to SQLite types
        if col_type in ("STRING", "VARCHAR", "CHAR"):
            col_type = "TEXT"
        elif col_type in ("INT", "INTEGER", "BIGINT", "SMALLINT"):
            col_type = "INTEGER"
        elif col_type in ("FLOAT", "DOUBLE", "DECIMAL", "NUMERIC"):
            col_type = "REAL"
        col_defs.append(f"{col_name} {col_type}")

    create_sql = f"CREATE TABLE {safe_table_name} ({', '.join(col_defs)})"
    cursor.execute(create_sql)

    # Insert rows
    if table_info.rows:
        placeholders = ", ".join("?" * len(table_info.columns))
        insert_sql = f"INSERT INTO {safe_table_name} VALUES ({placeholders})"
        cursor.executemany(insert_sql, table_info.rows)

    conn.commit()
    conn.close()

    return db_path
