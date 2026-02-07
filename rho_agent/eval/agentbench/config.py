"""Configuration and result dataclasses for evaluation."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    """Status of a task execution."""

    COMPLETED = "completed"
    AGENT_CONTEXT_LIMIT = "agent context limit"
    AGENT_VALIDATION_FAILED = "agent validation failed"
    AGENT_INVALID_ACTION = "agent invalid action"
    TASK_LIMIT_REACHED = "task limit reached"
    TASK_ERROR = "task error"
    UNKNOWN = "unknown"


class EvalAbortedError(Exception):
    """Raised when evaluation is aborted due to consecutive errors."""

    def __init__(self, message: str, consecutive_errors: int):
        super().__init__(message)
        self.consecutive_errors = consecutive_errors


@dataclass
class EvalConfig:
    """Configuration for running evaluations."""

    model: str = "gpt-5-mini"
    base_url: str | None = None
    max_turns: int = 20
    parallel: int = 1
    output_dir: str | None = None
    system_prompt_file: str | None = None
    verbose: bool = False
    max_consecutive_errors: int = 5  # Abort after this many consecutive task errors
    service_tier: str | None = None  # OpenAI service tier: "flex", "auto", or None


@dataclass
class DBBenchResult:
    """Result of a DBBench task evaluation."""

    is_correct: bool
    answer: str | None
    ground_truth: list[str]
    std_sql: str | None
    type: str  # SELECT, INSERT, UPDATE, DELETE


@dataclass
class OSResult:
    """Result of an OS interaction task evaluation."""

    result: bool  # True if correct


@dataclass
class TaskResult:
    """Result of a single task execution."""

    index: int
    status: TaskStatus
    history: list[dict[str, Any]]
    time: dict[str, Any]  # {"timestamp": int, "str": str}
    result: DBBenchResult | OSResult | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON output."""
        data: dict[str, Any] = {
            "index": self.index,
            "status": self.status.value,
            "history": self.history,
            "time": self.time,
        }
        if self.result is not None:
            if isinstance(self.result, DBBenchResult):
                data["result"] = {
                    "is_correct": self.result.is_correct,
                    "answer": self.result.answer,
                    "ground_truth": self.result.ground_truth,
                    "std_sql": self.result.std_sql,
                    "type": self.result.type,
                }
            else:
                data["result"] = {"result": self.result.result}
        if self.error:
            data["error"] = self.error
        return data

    @staticmethod
    def create_time() -> dict[str, Any]:
        """Create a time dictionary for the current time."""
        now = datetime.now()
        return {
            "timestamp": int(now.timestamp()),
            "str": now.strftime("%Y-%m-%d %H:%M:%S"),
        }


@dataclass
class EvalMetrics:
    """Aggregate metrics for an evaluation run."""

    total: int = 0
    passed: int = 0
    failed: int = 0

    # Status breakdown
    completed: int = 0
    context_limit: int = 0
    validation_failed: int = 0
    invalid_action: int = 0
    task_limit_reached: int = 0
    task_error: int = 0
    unknown: int = 0

    # History stats
    history_lengths: list[int] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        """Calculate accuracy (pass rate)."""
        return self.passed / self.total if self.total > 0 else 0.0

    @property
    def average_history_length(self) -> float:
        """Calculate average history length."""
        if not self.history_lengths:
            return 0.0
        return sum(self.history_lengths) / len(self.history_lengths)

    @property
    def max_history_length(self) -> int:
        """Get maximum history length."""
        return max(self.history_lengths) if self.history_lengths else 0

    @property
    def min_history_length(self) -> int:
        """Get minimum history length."""
        return min(self.history_lengths) if self.history_lengths else 0

    def add_result(self, result: TaskResult, is_correct: bool) -> None:
        """Add a task result to the metrics."""
        self.total += 1
        if is_correct:
            self.passed += 1
        else:
            self.failed += 1

        # Track status
        match result.status:
            case TaskStatus.COMPLETED:
                self.completed += 1
            case TaskStatus.AGENT_CONTEXT_LIMIT:
                self.context_limit += 1
            case TaskStatus.AGENT_VALIDATION_FAILED:
                self.validation_failed += 1
            case TaskStatus.AGENT_INVALID_ACTION:
                self.invalid_action += 1
            case TaskStatus.TASK_LIMIT_REACHED:
                self.task_limit_reached += 1
            case TaskStatus.TASK_ERROR:
                self.task_error += 1
            case _:
                self.unknown += 1

        # Track history length
        self.history_lengths.append(len(result.history))

    def to_dict(self) -> dict[str, Any]:
        """Convert to AgentBench-compatible output format."""
        return {
            "total": self.total,
            "validation": {
                "completed": self.completed / self.total if self.total > 0 else 0,
                "agent context limit": self.context_limit / self.total if self.total > 0 else 0,
                "agent validation failed": self.validation_failed / self.total
                if self.total > 0
                else 0,
                "agent invalid action": self.invalid_action / self.total if self.total > 0 else 0,
                "task limit reached": self.task_limit_reached / self.total if self.total > 0 else 0,
                "task error": self.task_error / self.total if self.total > 0 else 0,
                "unknown": self.unknown / self.total if self.total > 0 else 0,
                "average_history_length": self.average_history_length,
                "max_history_length": self.max_history_length,
                "min_history_length": self.min_history_length,
            },
            "custom": {
                "overall": {
                    "total": self.total,
                    "pass": self.passed,
                    "wrong": self.failed,
                    "acc": self.accuracy,
                }
            },
        }
