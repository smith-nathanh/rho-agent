"""Multi-turn worker agent with budget-aware handoff."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..core.agent import Agent
from ..core.session import Session
from .models import ConductorConfig, Task, TaskDAG, VerificationConfig
from .prompts import (
    WORKER_SYSTEM_PROMPT,
    WORKER_USER_TEMPLATE,
    WORKER_HANDOFF_PROMPT,
    WORKER_RETRY_TEMPLATE,
    WORKER_RESUME_TEMPLATE,
    format_acceptance_criteria,
    format_verification,
)


@dataclass
class SessionUsage:
    """Extracted token/cost usage from a session."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class WorkerResult:
    """Result from a single worker session."""

    status: str  # "completed", "handoff", "incomplete"
    text: str
    handoff_doc: str | None = None
    usage: SessionUsage = field(default_factory=SessionUsage)


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
        acceptance_criteria=format_acceptance_criteria(task.acceptance_criteria),
        working_dir=config.working_dir,
        prd_summary=f"Project: {dag.project_name}\n\n{prd_text}"
        if prd_text
        else f"Project: {dag.project_name}",
        verification_commands=format_verification(dag.verification),
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
        acceptance_criteria=format_acceptance_criteria(task.acceptance_criteria),
        verification_commands=format_verification(dag.verification),
    )


def _is_over_budget(
    session_tokens: int,
    context_window: int,
    threshold: float,
) -> bool:
    return session_tokens >= int(context_window * threshold)


def _is_task_complete(text: str) -> bool:
    return "TASK COMPLETE" in text.upper()


def _extract_usage(session: Session) -> SessionUsage:
    """Extract usage from a Session's state."""
    return SessionUsage(
        input_tokens=session.state.usage["input_tokens"],
        output_tokens=session.state.usage["output_tokens"],
        cost_usd=session.state.usage["cost_usd"],
    )


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
    agent = Agent(config.agent_config(system_prompt=WORKER_SYSTEM_PROMPT, profile="developer"))
    session = Session(agent)
    session.cancel_check = cancel_check

    if handoff_doc:
        prompt = _build_resume_prompt(task, dag, handoff_doc)
    else:
        prompt = _build_initial_prompt(task, dag, config, prd_text)

    status = "incomplete"
    result_text = ""
    result_handoff: str | None = None
    max_turns = max(1, config.max_worker_turns)

    turn_prompt = prompt
    for turn_num in range(1, max_turns + 1):
        result = await session.run(turn_prompt)
        result_text = result.text
        if _is_over_budget(
            session._last_input_tokens,
            config.context_window,
            config.budget_threshold,
        ):
            handoff_result = await session.run(WORKER_HANDOFF_PROMPT)
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

    return WorkerResult(
        status=status,
        text=result_text,
        handoff_doc=result_handoff,
        usage=_extract_usage(session),
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
    agent = Agent(config.agent_config(system_prompt=WORKER_SYSTEM_PROMPT, profile="developer"))
    session = Session(agent)
    session.cancel_check = cancel_check

    prompt = WORKER_RETRY_TEMPLATE.format(error_output=error_output)

    status = "incomplete"
    result = await session.run(prompt)
    if _is_task_complete(result.text):
        status = "completed"

    return WorkerResult(
        status=status,
        text=result.text,
        usage=_extract_usage(session),
    )
