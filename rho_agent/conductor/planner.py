"""PRD to task DAG planner using a single-turn readonly agent."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..runtime import create_runtime, run_prompt, session_usage
from ..runtime.types import SessionUsage
from .models import ConductorConfig, Task, TaskDAG, VerificationConfig
from .prompts import PLANNER_SYSTEM_PROMPT, PLANNER_USER_TEMPLATE


def _extract_json(text: str) -> dict[str, Any]:
    """Extract first JSON object from text."""
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("No JSON object found in planner output.")


async def _get_project_tree(working_dir: str, max_depth: int = 3) -> str:
    """Get a directory tree listing for context."""
    proc = await asyncio.create_subprocess_exec(
        "find",
        ".",
        "-maxdepth",
        str(max_depth),
        "-not",
        "-path",
        "./.git/*",
        "-not",
        "-path",
        "./.venv/*",
        "-not",
        "-path",
        "./__pycache__/*",
        "-not",
        "-name",
        "__pycache__",
        cwd=working_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode().strip()


def _build_dag(raw: dict[str, Any], config: ConductorConfig) -> TaskDAG:
    """Construct a TaskDAG from planner JSON output, applying config overrides."""
    raw_verification = raw.get("verification", {})
    verification = VerificationConfig(
        test_cmd=config.test_cmd or raw_verification.get("test_cmd"),
        lint_cmd=config.lint_cmd or raw_verification.get("lint_cmd"),
        typecheck_cmd=config.typecheck_cmd or raw_verification.get("typecheck_cmd"),
    )
    tasks: dict[str, Task] = {}
    for t in raw.get("tasks", []):
        task = Task(
            id=t["id"],
            title=t["title"],
            description=t["description"],
            acceptance_criteria=t.get("acceptance_criteria", []),
            depends_on=t.get("depends_on", []),
        )
        tasks[task.id] = task

    # Validate: all dependencies must reference existing task IDs
    all_ids = set(tasks.keys())
    for task in tasks.values():
        bad_deps = set(task.depends_on) - all_ids
        if bad_deps:
            raise ValueError(
                f"Task {task.id} depends on unknown tasks: {bad_deps}"
            )

    # Validate: no cycles (topological sort check)
    visited: set[str] = set()
    in_stack: set[str] = set()

    def _check_cycle(tid: str) -> None:
        if tid in in_stack:
            raise ValueError(f"Dependency cycle detected involving task {tid}")
        if tid in visited:
            return
        in_stack.add(tid)
        for dep in tasks[tid].depends_on:
            _check_cycle(dep)
        in_stack.discard(tid)
        visited.add(tid)

    for tid in tasks:
        _check_cycle(tid)

    return TaskDAG(
        project_name=raw.get("project_name", "unnamed"),
        tasks=tasks,
        verification=verification,
    )


async def run_planner(
    prd_text: str,
    config: ConductorConfig,
    *,
    cancel_check: callable | None = None,
) -> tuple[TaskDAG, dict[str, int]]:
    """Run the planner agent to decompose a PRD into a task DAG.

    Returns (TaskDAG, usage_dict).
    """
    options = config.runtime_options(
        profile="readonly",
        metadata={"source": "conductor_planner"},
    )
    runtime = create_runtime(
        PLANNER_SYSTEM_PROMPT,
        options=options,
        cancel_check=cancel_check,
    )

    project_tree = await _get_project_tree(config.working_dir)
    user_prompt = PLANNER_USER_TEMPLATE.format(
        prd_text=prd_text,
        project_tree=project_tree,
    )

    async with runtime:
        result = await run_prompt(runtime, user_prompt)
        raw = _extract_json(result.text)
        dag = _build_dag(raw, config)

    usage = session_usage(runtime.session)
    return dag, {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": usage.cost_usd,
    }
