"""Fresh-context reviewer that fixes issues and reruns tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..runtime import (
    RuntimeOptions,
    create_runtime,
    start_runtime,
    run_prompt,
    close_runtime,
)
from .models import ConductorConfig, Task, TaskDAG
from .prompts import REVIEWER_SYSTEM_PROMPT, REVIEWER_USER_TEMPLATE
from .worker import _format_acceptance_criteria, _format_verification


@dataclass
class ReviewResult:
    """Result from a reviewer session."""

    summary: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


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
    options = RuntimeOptions(
        model=config.model,
        profile="developer",
        working_dir=config.working_dir,
        auto_approve=True,
        enable_delegate=False,
        team_id=config.team_id,
        project_id=config.project_id,
        telemetry_metadata={"source": "conductor_reviewer", "task_id": task.id},
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

    status = "completed"
    await start_runtime(runtime)
    try:
        result = await run_prompt(runtime, prompt)
    except Exception:
        status = "error"
        raise
    finally:
        await close_runtime(runtime, status)

    return ReviewResult(
        summary=result.text,
        input_tokens=runtime.session.total_input_tokens,
        output_tokens=runtime.session.total_output_tokens,
        cost_usd=runtime.session.total_cost_usd,
    )
