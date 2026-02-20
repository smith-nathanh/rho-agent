"""Configuration and result dataclasses for BIRD-Bench evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .evaluator import BirdResult


class TaskStatus(str, Enum):
    """Possible outcomes for a single evaluation task."""

    COMPLETED = "completed"
    AGENT_CONTEXT_LIMIT = "agent context limit"
    TASK_LIMIT_REACHED = "task limit reached"
    TASK_ERROR = "task error"


class EvalAbortedError(Exception):
    """Raised when too many consecutive errors abort the evaluation."""

    def __init__(self, message: str, consecutive_errors: int) -> None:
        super().__init__(message)
        self.consecutive_errors = consecutive_errors


@dataclass
class EvalConfig:
    """Runtime configuration for a BIRD-Bench evaluation run."""

    model: str = "gpt-5-mini"
    base_url: str | None = None
    max_turns: int = 20
    parallel: int = 1
    output_dir: str | None = None
    system_prompt_file: str | None = None
    verbose: bool = False
    max_consecutive_errors: int = 5
    service_tier: str | None = None


@dataclass
class TaskResult:
    """Result of running a single BIRD-Bench task through the agent."""

    index: int
    status: TaskStatus
    history: list[dict[str, Any]]
    time: dict[str, Any]
    result: BirdResult | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        data: dict[str, Any] = {
            "index": self.index,
            "status": self.status.value,
            "history": self.history,
            "time": self.time,
        }
        if self.result is not None:
            data["result"] = {
                "is_correct": self.result.is_correct,
                "predicted_sql": self.result.predicted_sql,
                "gold_sql": self.result.gold_sql,
                "predicted_result": self.result.predicted_result,
                "gold_result": self.result.gold_result,
                "error": self.result.error,
                "difficulty": self.result.difficulty,
                "db_id": self.result.db_id,
            }
        if self.error:
            data["error"] = self.error
        return data

    @staticmethod
    def create_time() -> dict[str, Any]:
        """Return a timestamp dict with epoch and human-readable values."""
        now = datetime.now()
        return {
            "timestamp": int(now.timestamp()),
            "str": now.strftime("%Y-%m-%d %H:%M:%S"),
        }


@dataclass
class BirdMetrics:
    """Aggregate metrics for a BIRD-Bench evaluation run.

    Tracks accuracy overall and broken down by difficulty and database.
    """

    total: int = 0
    passed: int = 0
    failed: int = 0

    # Status breakdown
    completed: int = 0
    context_limit: int = 0
    task_limit_reached: int = 0
    task_error: int = 0

    # Per-difficulty counts
    difficulty_total: dict[str, int] = field(default_factory=dict)
    difficulty_passed: dict[str, int] = field(default_factory=dict)

    # Per-database counts
    db_total: dict[str, int] = field(default_factory=dict)
    db_passed: dict[str, int] = field(default_factory=dict)

    # History stats
    history_lengths: list[int] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.passed / self.total if self.total > 0 else 0.0

    def add_result(self, result: TaskResult, is_correct: bool) -> None:
        self.total += 1
        if is_correct:
            self.passed += 1
        else:
            self.failed += 1

        # Status tracking
        match result.status:
            case TaskStatus.COMPLETED:
                self.completed += 1
            case TaskStatus.AGENT_CONTEXT_LIMIT:
                self.context_limit += 1
            case TaskStatus.TASK_LIMIT_REACHED:
                self.task_limit_reached += 1
            case TaskStatus.TASK_ERROR:
                self.task_error += 1

        self.history_lengths.append(len(result.history))

        # Per-difficulty tracking
        if result.result:
            diff = result.result.difficulty
            self.difficulty_total[diff] = self.difficulty_total.get(diff, 0) + 1
            if is_correct:
                self.difficulty_passed[diff] = self.difficulty_passed.get(diff, 0) + 1

            db = result.result.db_id
            self.db_total[db] = self.db_total.get(db, 0) + 1
            if is_correct:
                self.db_passed[db] = self.db_passed.get(db, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        avg_history = (
            sum(self.history_lengths) / len(self.history_lengths) if self.history_lengths else 0.0
        )

        # Difficulty breakdown
        difficulty_breakdown = {}
        for diff in sorted(self.difficulty_total):
            t = self.difficulty_total[diff]
            p = self.difficulty_passed.get(diff, 0)
            difficulty_breakdown[diff] = {
                "total": t,
                "passed": p,
                "accuracy": p / t if t > 0 else 0.0,
            }

        # Database breakdown
        db_breakdown = {}
        for db in sorted(self.db_total):
            t = self.db_total[db]
            p = self.db_passed.get(db, 0)
            db_breakdown[db] = {
                "total": t,
                "passed": p,
                "accuracy": p / t if t > 0 else 0.0,
            }

        return {
            "total": self.total,
            "overall": {
                "passed": self.passed,
                "failed": self.failed,
                "accuracy": self.accuracy,
            },
            "status": {
                "completed": self.completed,
                "context_limit": self.context_limit,
                "task_limit_reached": self.task_limit_reached,
                "task_error": self.task_error,
            },
            "by_difficulty": difficulty_breakdown,
            "by_database": db_breakdown,
            "history": {
                "average_length": avg_history,
                "min_length": min(self.history_lengths) if self.history_lengths else 0,
                "max_length": max(self.history_lengths) if self.history_lengths else 0,
            },
        }
