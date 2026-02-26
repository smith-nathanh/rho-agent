"""CLI command for the continuum module."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated

import typer

from .models import DEFAULT_MODEL, ContinuumConfig


def continuum(
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
    ] = DEFAULT_MODEL,
    service_tier: Annotated[
        str | None,
        typer.Option("--service-tier", help="OpenAI service tier (flex, auto)"),
    ] = os.getenv("RHO_AGENT_SERVICE_TIER"),
    context_window: Annotated[
        int,
        typer.Option("--context-window", help="Context window size"),
    ] = 400_000,
    budget_threshold: Annotated[
        float,
        typer.Option("--budget-threshold", help="Context budget threshold (0-1)"),
    ] = 0.7,
    max_sessions: Annotated[
        int,
        typer.Option("--max-sessions", help="Max sessions before pausing"),
    ] = 10,
    test_cmd: Annotated[
        str | None,
        typer.Option("--test-cmd", help="Test command"),
    ] = None,
    lint_cmd: Annotated[
        str | None,
        typer.Option("--lint-cmd", help="Lint command"),
    ] = None,
    typecheck_cmd: Annotated[
        str | None,
        typer.Option("--typecheck-cmd", help="Typecheck command"),
    ] = None,
    git_branch: Annotated[
        str | None,
        typer.Option("--branch", help="Create and use a git branch"),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Resume from saved state"),
    ] = False,
    state_path: Annotated[
        str | None,
        typer.Option("--state", help="Path to state JSON file"),
    ] = None,
    project_id: Annotated[
        str | None,
        typer.Option("--project-id", help="Telemetry project ID"),
    ] = None,
    team_id: Annotated[
        str | None,
        typer.Option("--team-id", help="Telemetry team ID"),
    ] = None,
) -> None:
    """Run a continuity-first agent loop to implement a PRD."""
    resolved_dir = str(Path(working_dir).expanduser().resolve())
    config = ContinuumConfig(
        prd_path=prd,
        working_dir=resolved_dir,
        model=model,
        service_tier=service_tier,
        context_window=context_window,
        budget_threshold=budget_threshold,
        max_sessions=max_sessions,
        test_cmd=test_cmd,
        lint_cmd=lint_cmd,
        typecheck_cmd=typecheck_cmd,
        git_branch=git_branch,
        resume=resume,
        state_path=state_path,
        project_id=project_id,
        team_id=team_id,
    )

    from .loop import run_continuum

    state = asyncio.run(run_continuum(config))
    if state.status == "error":
        raise typer.Exit(1)
    if state.status == "paused":
        raise typer.Exit(3)
