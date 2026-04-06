"""CLI command for the evolve module."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv

load_dotenv()

import typer

from .models import DEFAULT_MODEL, EvolveConfig


def evolve(
    harness: Annotated[
        str,
        typer.Argument(help="Dotted path to DomainHarness subclass"),
    ],
    run_dir: Annotated[
        str,
        typer.Option("--run-dir", "-d", help="Output directory for evolution runs"),
    ] = "./evolve-runs",
    model: Annotated[
        str,
        typer.Option("--model", "-m", help="Meta-agent model"),
    ] = DEFAULT_MODEL,
    task_model: Annotated[
        str | None,
        typer.Option("--task-model", help="Task-agent model (default: inherit from --model)"),
    ] = None,
    max_generations: Annotated[
        int,
        typer.Option("--max-generations", "-n", help="Maximum number of generations"),
    ] = 20,
    parallel: Annotated[
        int,
        typer.Option("--parallel", "-p", help="Concurrent eval tasks"),
    ] = 1,
    seed: Annotated[
        str | None,
        typer.Option("--seed", help="Path to seed workspace directory"),
    ] = None,
    staged_sample: Annotated[
        int,
        typer.Option("--staged-sample", help="Number of quick-filter scenarios"),
    ] = 3,
    harness_arg: Annotated[
        list[str] | None,
        typer.Option("--harness-arg", help="key=value pairs passed to harness constructor"),
    ] = None,
    daytona: Annotated[
        bool,
        typer.Option("--daytona/--no-daytona", help="Run meta-agent in Daytona sandbox"),
    ] = False,
    parent_strategy: Annotated[
        str,
        typer.Option("--parent-strategy", help="Parent selection strategy"),
    ] = "tournament",
    meta_timeout: Annotated[
        int,
        typer.Option("--meta-timeout", help="Meta-agent wall-clock timeout in seconds"),
    ] = 3600,
) -> None:
    """Run an evolutionary loop to build and improve task-agents."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    harness_kwargs: dict[str, str] = {}
    if harness_arg:
        for arg in harness_arg:
            if "=" not in arg:
                raise typer.BadParameter(f"Expected key=value format: {arg}")
            key, value = arg.split("=", 1)
            harness_kwargs[key] = value

    resolved_dir = str(Path(run_dir).expanduser().resolve())

    daytona_backend = None
    if daytona:
        from ..tools.handlers.daytona.backend import DaytonaBackend

        daytona_backend = DaytonaBackend()

    config = EvolveConfig(
        harness=harness,
        run_dir=resolved_dir,
        model=model,
        task_model=task_model,
        max_generations=max_generations,
        parallel=parallel,
        seed_workspace=seed,
        staged_sample_n=staged_sample,
        harness_kwargs=harness_kwargs,
        daytona_backend=daytona_backend,
        parent_strategy=parent_strategy,
        meta_timeout=meta_timeout,
    )

    from .loop import run_evolve

    generations = asyncio.run(run_evolve(config))

    scored = [g for g in generations if g.score is not None]
    if scored:
        best = max(scored, key=lambda g: g.score)  # type: ignore[arg-type]
        typer.echo(f"\nBest: {best.gen_id} (score={best.score:.4f})")
        typer.echo(f"Workspace: {best.workspace_path}")
    else:
        typer.echo("\nNo scored generations.")
        raise typer.Exit(1)
