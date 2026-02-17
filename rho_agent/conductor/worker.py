"""Multi-turn worker agent with budget-aware handoff."""

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
from .models import ConductorConfig, Task, TaskDAG, VerificationConfig
from .prompts import (
    WORKER_SYSTEM_PROMPT,
    WORKER_USER_TEMPLATE,
    WORKER_HANDOFF_PROMPT,
    WORKER_RETRY_TEMPLATE,
    WORKER_RESUME_TEMPLATE,
)


@dataclass
class WorkerResult:
    """Result from a single worker session."""

    status: str  # "completed", "handoff", "incomplete"
    text: str
    handoff_doc: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


def _format_verification(verification: VerificationConfig) -> str:
    lines = []
    if verification.test_cmd:
        lines.append(f"- Test: `{verification.test_cmd}`")
    if verification.lint_cmd:
        lines.append(f"- Lint: `{verification.lint_cmd}`")
    if verification.typecheck_cmd:
        lines.append(f"- Typecheck: `{verification.typecheck_cmd}`")
    return "\n".join(lines) if lines else "No verification commands configured."


def _format_acceptance_criteria(criteria: list[str]) -> str:
    return "\n".join(f"- {c}" for c in criteria)


def _build_initial_prompt(
    task: Task,
    dag: TaskDAG,
    config: ConductorConfig,
    prd_text: str,
) -> str:
    return WORKER_USER_TEMPLATE.format(
        task_id=task.id,
        task_title=task.title,
        task_description=task.description,
        acceptance_criteria=_format_acceptance_criteria(task.acceptance_criteria),
        working_dir=config.working_dir,
        prd_summary=f"Project: {dag.project_name}\n\n{prd_text}" if prd_text else f"Project: {dag.project_name}",
        verification_commands=_format_verification(dag.verification),
    )


def _build_resume_prompt(
    task: Task,
    dag: TaskDAG,
    handoff_doc: str,
) -> str:
    return WORKER_RESUME_TEMPLATE.format(
        handoff_doc=handoff_doc,
        task_id=task.id,
        task_title=task.title,
        task_description=task.description,
        acceptance_criteria=_format_acceptance_criteria(task.acceptance_criteria),
        verification_commands=_format_verification(dag.verification),
    )


def _is_over_budget(
    session_tokens: int,
    context_window: int,
    threshold: float,
) -> bool:
    return session_tokens >= int(context_window * threshold)


def _is_task_complete(text: str) -> bool:
    return "TASK COMPLETE" in text.upper()


async def run_worker(
    task: Task,
    dag: TaskDAG,
    *,
    config: ConductorConfig,
    prd_text: str = "",
    handoff_doc: str | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> WorkerResult:
    """Run a worker session for a task.

    Returns WorkerResult with status "completed" or "handoff".
    The handoff loop (retrying with fresh sessions) is managed by the scheduler.
    """
    options = RuntimeOptions(
        model=config.model,
        service_tier=config.service_tier,
        profile="developer",
        working_dir=config.working_dir,
        auto_approve=True,
        enable_delegate=False,
        team_id=config.team_id,
        project_id=config.project_id,
        telemetry_metadata={"source": "conductor_worker", "task_id": task.id},
    )
    runtime = create_runtime(
        WORKER_SYSTEM_PROMPT,
        options=options,
        cancel_check=cancel_check,
    )

    if handoff_doc:
        prompt = _build_resume_prompt(task, dag, handoff_doc)
    else:
        prompt = _build_initial_prompt(task, dag, config, prd_text)

    status = "incomplete"
    result_text = ""
    result_handoff: str | None = None
    max_turns = max(1, config.max_worker_turns)

    await start_runtime(runtime)
    try:
        turn_prompt = prompt
        for turn_num in range(1, max_turns + 1):
            result = await run_prompt(runtime, turn_prompt)
            result_text = result.text
            if _is_over_budget(
                runtime.session.last_input_tokens,
                config.context_window,
                config.budget_threshold,
            ):
                handoff_result = await run_prompt(runtime, WORKER_HANDOFF_PROMPT)
                result_handoff = handoff_result.text
                status = "handoff"
                break

            if _is_task_complete(result.text):
                status = "completed"
                break

            if turn_num < max_turns:
                turn_prompt = (
                    "You did not explicitly say 'TASK COMPLETE'. "
                    "Continue working on this same task and say "
                    "'TASK COMPLETE' only when fully done."
                )
    except Exception:
        status = "error"
        raise
    finally:
        await close_runtime(runtime, status)

    return WorkerResult(
        status=status,
        text=result_text,
        handoff_doc=result_handoff,
        input_tokens=runtime.session.total_input_tokens,
        output_tokens=runtime.session.total_output_tokens,
        cost_usd=runtime.session.total_cost_usd,
    )


async def run_worker_retry(
    task: Task,
    dag: TaskDAG,
    error_output: str,
    *,
    config: ConductorConfig,
    cancel_check: Callable[[], bool] | None = None,
) -> WorkerResult:
    """Run a fresh worker session to fix check failures."""
    options = RuntimeOptions(
        model=config.model,
        service_tier=config.service_tier,
        profile="developer",
        working_dir=config.working_dir,
        auto_approve=True,
        enable_delegate=False,
        team_id=config.team_id,
        project_id=config.project_id,
        telemetry_metadata={
            "source": "conductor_worker_retry",
            "task_id": task.id,
        },
    )
    runtime = create_runtime(
        WORKER_SYSTEM_PROMPT,
        options=options,
        cancel_check=cancel_check,
    )

    prompt = WORKER_RETRY_TEMPLATE.format(error_output=error_output)

    status = "incomplete"
    await start_runtime(runtime)
    try:
        result = await run_prompt(runtime, prompt)
        if _is_task_complete(result.text):
            status = "completed"
    except Exception:
        status = "error"
        raise
    finally:
        await close_runtime(runtime, status)

    return WorkerResult(
        status=status,
        text=result.text,
        input_tokens=runtime.session.total_input_tokens,
        output_tokens=runtime.session.total_output_tokens,
        cost_usd=runtime.session.total_cost_usd,
    )
