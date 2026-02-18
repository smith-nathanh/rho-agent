"""Fresh-context reviewer that fixes issues and reruns tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..runtime import create_runtime, run_prompt, session_usage
from ..runtime.types import SessionUsage
from .models import ConductorConfig, Task, TaskDAG
from .prompts import REVIEWER_SYSTEM_PROMPT, REVIEWER_USER_TEMPLATE
from .worker import _format_acceptance_criteria, _format_verification


@dataclass
class ReviewResult:
    """Result from a reviewer session."""

    summary: str
    usage: SessionUsage = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = SessionUsage()


async def run_reviewer(
    task: Task,
    dag: TaskDAG,
    diff_text: str,
    *,
    config: ConductorConfig,
    cancel_check: Callable[[], bool] | None = None,
) -> ReviewResult:
    """Run a fresh-context reviewer on the task diff.

    The reviewer has developer profile access — it can read files, edit code,
    and run verification commands. It fixes any issues it finds directly.
    """
    options = config.runtime_options(
        profile="developer",
        metadata={"source": "conductor_reviewer", "task_id": task.id},
    )
    runtime = create_runtime(
        REVIEWER_SYSTEM_PROMPT,
        options=options,
        cancel_check=cancel_check,
    )

    prompt = REVIEWER_USER_TEMPLATE.format(
        task_id=task.id,
        task_title=task.title,
        acceptance_criteria=_format_acceptance_criteria(task.acceptance_criteria),
        diff_text=diff_text,
        verification_commands=_format_verification(dag.verification),
    )

    async with runtime:
        result = await run_prompt(runtime, prompt)

    return ReviewResult(
        summary=result.text,
        usage=session_usage(runtime.session),
    )
