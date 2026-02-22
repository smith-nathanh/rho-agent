"""Fresh-context reviewer that fixes issues and reruns tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..core.agent import Agent
from ..core.session import Session
from .models import ConductorConfig, Task, TaskDAG
from .worker import SessionUsage
from .prompts import (
    REVIEWER_SYSTEM_PROMPT,
    REVIEWER_USER_TEMPLATE,
    format_acceptance_criteria,
    format_verification,
)


@dataclass
class ReviewResult:
    """Result from a reviewer session."""

    summary: str
    usage: SessionUsage = field(default_factory=SessionUsage)


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
    agent = Agent(config.agent_config(system_prompt=REVIEWER_SYSTEM_PROMPT, profile="developer"))
    session = Session(agent)
    session.cancel_check = cancel_check

    prompt = REVIEWER_USER_TEMPLATE.format(
        task_id=task.id,
        task_title=task.title,
        acceptance_criteria=format_acceptance_criteria(task.acceptance_criteria),
        diff_text=diff_text,
        verification_commands=format_verification(dag.verification),
    )

    result = await session.run(prompt)

    usage = session.state.usage
    return ReviewResult(
        summary=result.text,
        usage=SessionUsage(
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cost_usd=usage["cost_usd"],
        ),
    )
