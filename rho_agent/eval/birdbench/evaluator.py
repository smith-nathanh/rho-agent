"""Execution-based SQL evaluator for BIRD-Bench."""

from __future__ import annotations

import sqlite3
import signal
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BirdResult:
    """Result of evaluating a single BIRD-Bench task."""

    is_correct: bool
    predicted_sql: str | None  # SQL the agent submitted
    gold_sql: str  # reference SQL
    predicted_result: str | None  # stringified query result
    gold_result: str | None  # stringified query result
    error: str | None  # SQL execution error if any
    difficulty: str  # simple/moderate/challenging
    db_id: str  # which database


class BirdEvaluator:
    """Evaluates predicted SQL against gold SQL using execution accuracy."""

    def __init__(self, timeout: int = 30) -> None:
        """Initialize the evaluator.

        Args:
            timeout: Per-query execution timeout in seconds.
        """
        self.timeout = timeout

    def evaluate(
        self,
        predicted_sql: str | None,
        gold_sql: str,
        db_path: str | Path,
        difficulty: str,
        db_id: str,
    ) -> BirdResult:
        """Execute both SQLs against the database and compare results.

        Opens a read-only connection to the original database.

        Args:
            predicted_sql: The agent's submitted SQL (None if not submitted).
            gold_sql: The reference SQL.
            db_path: Path to the SQLite database file.
            difficulty: Task difficulty level.
            db_id: Database identifier.

        Returns:
            BirdResult with comparison outcome.
        """
        if predicted_sql is None:
            return BirdResult(
                is_correct=False,
                predicted_sql=None,
                gold_sql=gold_sql,
                predicted_result=None,
                gold_result=None,
                error="No SQL submitted",
                difficulty=difficulty,
                db_id=db_id,
            )

        # Open read-only connection via URI
        db_uri = f"file:{Path(db_path).resolve()}?mode=ro"
        try:
            conn = sqlite3.connect(db_uri, uri=True)
        except sqlite3.OperationalError as e:
            return BirdResult(
                is_correct=False,
                predicted_sql=predicted_sql,
                gold_sql=gold_sql,
                predicted_result=None,
                gold_result=None,
                error=f"Cannot open database: {e}",
                difficulty=difficulty,
                db_id=db_id,
            )

        try:
            # Execute gold SQL
            gold_result = self._execute_with_timeout(conn, gold_sql)
            if gold_result is None:
                return BirdResult(
                    is_correct=False,
                    predicted_sql=predicted_sql,
                    gold_sql=gold_sql,
                    predicted_result=None,
                    gold_result=None,
                    error="Gold SQL timed out",
                    difficulty=difficulty,
                    db_id=db_id,
                )

            if isinstance(gold_result, Exception):
                return BirdResult(
                    is_correct=False,
                    predicted_sql=predicted_sql,
                    gold_sql=gold_sql,
                    predicted_result=None,
                    gold_result=None,
                    error=f"Gold SQL error (dataset issue): {gold_result}",
                    difficulty=difficulty,
                    db_id=db_id,
                )

            # Execute predicted SQL
            predicted_result = self._execute_with_timeout(conn, predicted_sql)
            if predicted_result is None:
                return BirdResult(
                    is_correct=False,
                    predicted_sql=predicted_sql,
                    gold_sql=gold_sql,
                    predicted_result=None,
                    gold_result=_stringify(gold_result),
                    error="Predicted SQL timed out",
                    difficulty=difficulty,
                    db_id=db_id,
                )

            if isinstance(predicted_result, Exception):
                return BirdResult(
                    is_correct=False,
                    predicted_sql=predicted_sql,
                    gold_sql=gold_sql,
                    predicted_result=None,
                    gold_result=_stringify(gold_result),
                    error=f"Predicted SQL error: {predicted_result}",
                    difficulty=difficulty,
                    db_id=db_id,
                )

            # Compare result sets (order-insensitive)
            is_correct = _compare_results(predicted_result, gold_result)

            return BirdResult(
                is_correct=is_correct,
                predicted_sql=predicted_sql,
                gold_sql=gold_sql,
                predicted_result=_stringify(predicted_result),
                gold_result=_stringify(gold_result),
                error=None,
                difficulty=difficulty,
                db_id=db_id,
            )

        finally:
            conn.close()

    def _execute_with_timeout(
        self,
        conn: sqlite3.Connection,
        sql: str,
    ) -> list[tuple] | Exception | None:
        """Execute SQL with a timeout.

        Returns:
            List of result tuples on success.
            Exception instance on SQL error.
            None on timeout.
        """

        def _timeout_handler(signum, frame):
            raise TimeoutError()

        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        try:
            signal.alarm(self.timeout)
            cursor = conn.cursor()
            cursor.execute(sql)
            result = cursor.fetchall()
            signal.alarm(0)
            return result
        except TimeoutError:
            return None
        except Exception as e:
            signal.alarm(0)
            return e
        finally:
            signal.signal(signal.SIGALRM, old_handler)


def _normalize_value(val: object) -> object:
    """Normalize a value for comparison."""
    if val is None:
        return None
    # Normalize numeric types for consistent comparison
    if isinstance(val, float):
        # Round to avoid floating point comparison issues
        return round(val, 6)
    return val


def _compare_results(
    predicted: list[tuple],
    gold: list[tuple],
) -> bool:
    """Compare two result sets (order-insensitive).

    Both results are treated as sets of tuples. NULLs are compared
    as equal to each other.
    """
    # Normalize values in both result sets
    pred_normalized = [tuple(_normalize_value(v) for v in row) for row in predicted]
    gold_normalized = [tuple(_normalize_value(v) for v in row) for row in gold]

    # Compare as multisets (sorted lists of tuples)
    # Use a key function that handles None comparison
    def sort_key(row):
        return tuple((0, "") if v is None else (1, v) for v in row)

    try:
        pred_sorted = sorted(pred_normalized, key=sort_key)
        gold_sorted = sorted(gold_normalized, key=sort_key)
        return pred_sorted == gold_sorted
    except TypeError:
        # Fallback: compare as multisets using Counter-style comparison
        # This handles cases where values aren't directly sortable
        from collections import Counter

        return Counter(pred_normalized) == Counter(gold_normalized)


def _stringify(result: list[tuple[object, ...]], max_rows: int = 20) -> str:
    """Convert query result to a string for logging."""
    if not result:
        return "(empty result set)"

    lines = []
    for i, row in enumerate(result):
        if i >= max_rows:
            lines.append(f"... ({len(result) - max_rows} more rows)")
            break
        lines.append(str(row))
    return "\n".join(lines)
