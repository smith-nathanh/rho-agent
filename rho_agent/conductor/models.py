"""Data models for the conductor module."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Task:
    """A single unit of work in the task DAG."""

    id: str
    title: str
    description: str
    acceptance_criteria: list[str]
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    commit_sha: str | None = None
    review_sha: str | None = None
    attempts: int = 0
    handoff_doc: str | None = None
    error: str | None = None


@dataclass
class VerificationConfig:
    """Commands for automated checks between worker and reviewer."""

    test_cmd: str | None = None
    lint_cmd: str | None = None
    typecheck_cmd: str | None = None


@dataclass
class TaskDAG:
    """Directed acyclic graph of tasks produced by the planner."""

    project_name: str
    tasks: dict[str, Task]
    verification: VerificationConfig

    def ready_tasks(self) -> list[Task]:
        """Return PENDING tasks whose dependencies are all DONE."""
        ready = []
        for task in self.tasks.values():
            if task.status != TaskStatus.PENDING:
                continue
            if all(
                dep in self.tasks and self.tasks[dep].status == TaskStatus.DONE
                for dep in task.depends_on
            ):
                ready.append(task)
        return ready

    def next_ready_task(self) -> Task | None:
        """Return the first ready task (sorted by id)."""
        ready = self.ready_tasks()
        if not ready:
            return None
        return sorted(ready, key=lambda t: t.id)[0]

    def all_done(self) -> bool:
        return bool(self.tasks) and all(
            t.status == TaskStatus.DONE for t in self.tasks.values()
        )

    def has_remaining_work(self) -> bool:
        """True if there are tasks that could still run (not DONE, not FAILED)."""
        return any(
            t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)
            for t in self.tasks.values()
        )


@dataclass
class TaskUsage:
    """Token usage tracking for a single task."""

    task_id: str
    worker_input_tokens: int = 0
    worker_output_tokens: int = 0
    worker_cost_usd: float = 0.0
    worker_sessions: int = 0
    reviewer_input_tokens: int = 0
    reviewer_output_tokens: int = 0
    reviewer_cost_usd: float = 0.0


@dataclass
class ConductorConfig:
    """Configuration for a conductor run."""

    prd_path: str
    working_dir: str = "."
    model: str = field(
        default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-5-mini")
    )
    state_path: str | None = None
    context_window: int = 400_000
    budget_threshold: float = 0.7
    max_worker_turns: int = 3
    max_worker_sessions: int = 3
    max_task_attempts: int = 3
    test_cmd: str | None = None
    lint_cmd: str | None = None
    typecheck_cmd: str | None = None
    enable_reviewer: bool = True
    git_branch: str | None = None
    resume: bool = False
    project_id: str | None = None
    team_id: str | None = None


@dataclass
class ConductorState:
    """JSON-serializable persistence envelope."""

    run_id: str
    config: ConductorConfig
    dag: TaskDAG | None = None
    usage: dict[str, TaskUsage] = field(default_factory=dict)
    status: str = "running"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConductorState:
        config = ConductorConfig(**data["config"])
        dag = None
        if data.get("dag"):
            dag_data = data["dag"]
            tasks = {
                k: Task(
                    **{**v, "status": TaskStatus(v["status"])}
                )
                for k, v in dag_data["tasks"].items()
            }
            verification = VerificationConfig(**dag_data["verification"])
            dag = TaskDAG(
                project_name=dag_data["project_name"],
                tasks=tasks,
                verification=verification,
            )
        usage = {
            k: TaskUsage(**v) for k, v in data.get("usage", {}).items()
        }
        return cls(
            run_id=data["run_id"],
            config=config,
            dag=dag,
            usage=usage,
            status=data.get("status", "running"),
        )
