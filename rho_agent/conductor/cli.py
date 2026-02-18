"""CLI command for the conductor module."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated

import typer

from .models import ConductorConfig


def conduct(
    prd: Annotated[
        str,
        typer.Argument(help="Path to the PRD markdown file"),
    ],
    working_dir: Annotated[
        str,
        typer.Option("--working-dir", "-d", help="Working directory for the project"),
    ] = ".",
    model: Annotated[
        str,
        typer.Option("--model", "-m", help="Model to use"),
    ] = os.getenv("OPENAI_MODEL", "gpt-5-mini"),
    service_tier: Annotated[
        str | None,
        typer.Option("--service-tier", help="OpenAI service tier (flex, auto)"),
    ] = os.getenv("RHO_AGENT_SERVICE_TIER"),
    state_path: Annotated[
        str | None,
        typer.Option("--state", help="Path to state JSON file"),
    ] = None,
    context_window: Annotated[
        int,
        typer.Option("--context-window", help="Context window size"),
    ] = 400_000,
    budget_threshold: Annotated[
        float,
        typer.Option("--budget-threshold", help="Context budget threshold (0-1)"),
    ] = 0.7,
    max_worker_turns: Annotated[
        int,
        typer.Option(
            "--max-worker-turns",
            help="Max model turns per worker session",
        ),
    ] = 3,
    max_worker_sessions: Annotated[
        int,
        typer.Option(
            "--max-worker-sessions",
            help="Max worker sessions/handoffs per task before pausing",
        ),
    ] = 3,
    max_task_attempts: Annotated[
        int,
        typer.Option("--max-task-attempts", help="Max retries on check failure"),
    ] = 3,
    test_cmd: Annotated[
        str | None,
        typer.Option("--test-cmd", help="Override test command"),
    ] = None,
    lint_cmd: Annotated[
        str | None,
        typer.Option("--lint-cmd", help="Override lint command"),
    ] = None,
    typecheck_cmd: Annotated[
        str | None,
        typer.Option("--typecheck-cmd", help="Override typecheck command"),
    ] = None,
    no_reviewer: Annotated[
        bool,
        typer.Option("--no-reviewer", help="Disable the reviewer gate"),
    ] = False,
    git_branch: Annotated[
        str | None,
        typer.Option("--branch", help="Create and use a git branch"),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Resume from saved state"),
    ] = False,
    project_id: Annotated[
        str | None,
        typer.Option("--project-id", help="Telemetry project ID"),
    ] = None,
    team_id: Annotated[
        str | None,
        typer.Option("--team-id", help="Telemetry team ID"),
    ] = None,
) -> None:
    """Run a single-agent conductor to implement a PRD."""
    resolved_dir = str(Path(working_dir).expanduser().resolve())
    config = ConductorConfig(
        prd_path=prd,
        working_dir=resolved_dir,
        model=model,
        service_tier=service_tier,
        state_path=state_path,
        context_window=context_window,
        budget_threshold=budget_threshold,
        max_worker_turns=max_worker_turns,
        max_worker_sessions=max_worker_sessions,
        max_task_attempts=max_task_attempts,
        test_cmd=test_cmd,
        lint_cmd=lint_cmd,
        typecheck_cmd=typecheck_cmd,
        enable_reviewer=not no_reviewer,
        git_branch=git_branch,
        resume=resume,
        project_id=project_id,
        team_id=team_id,
    )

    from .scheduler import run_conductor

    state = asyncio.run(run_conductor(config))
    if state.status == "error":
        raise typer.Exit(1)
    if state.status == "failed":
        raise typer.Exit(2)
    if state.status == "paused_user_attention":
        raise typer.Exit(3)
