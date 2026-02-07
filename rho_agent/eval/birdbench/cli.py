"""CLI for running BIRD-Bench evaluations."""

import asyncio
import os
from pathlib import Path
from typing import Annotated, Optional

from dotenv import load_dotenv

load_dotenv()

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from .config import EvalAbortedError, EvalConfig
from .output import (
    create_run_dir,
    format_summary,
    get_completed_indices,
    rebuild_metrics_from_runs,
    save_run_config,
)
from .runner import BirdRunner
from .task import load_bird_tasks

console = Console()


def bird(
    data_file: Annotated[
        str,
        typer.Argument(help="Path to BIRD task JSON file (e.g., mini_dev_sqlite.json)"),
    ],
    db_dir: Annotated[
        str,
        typer.Argument(help="Path to dev_databases/ directory"),
    ],
    model: Annotated[
        str,
        typer.Option("--model", "-m", help="Model to use"),
    ] = os.getenv("OPENAI_MODEL", "gpt-5-mini"),
    base_url: Annotated[
        Optional[str],
        typer.Option("--base-url", help="API base URL"),
    ] = os.getenv("OPENAI_BASE_URL"),
    max_turns: Annotated[
        int,
        typer.Option("--max-turns", help="Maximum turns per task"),
    ] = 20,
    parallel: Annotated[
        int,
        typer.Option("--parallel", "-p", help="Number of parallel tasks"),
    ] = 1,
    output: Annotated[
        Optional[str],
        typer.Option("--output", "-o", help="Output directory"),
    ] = None,
    resume: Annotated[
        Optional[str],
        typer.Option("--resume", "-r", help="Resume from previous run directory"),
    ] = None,
    system_prompt: Annotated[
        Optional[str],
        typer.Option("--system-prompt", help="Path to custom system prompt file"),
    ] = None,
    limit: Annotated[
        Optional[int],
        typer.Option("--limit", "-n", help="Limit number of tasks"),
    ] = None,
    offset: Annotated[
        int,
        typer.Option("--offset", help="Skip first N tasks"),
    ] = 0,
    difficulty: Annotated[
        Optional[str],
        typer.Option("--difficulty", help="Filter by difficulty: simple, moderate, challenging"),
    ] = None,
    no_evidence: Annotated[
        bool,
        typer.Option("--no-evidence", help="Withhold evidence hints (harder mode)"),
    ] = False,
    service_tier: Annotated[
        Optional[str],
        typer.Option("--service-tier", help="OpenAI service tier (flex, auto)"),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Verbose output"),
    ] = False,
) -> None:
    """Run BIRD-Bench text-to-SQL evaluation.

    Databases are copied to temp files for each task — the agent cannot
    corrupt your original database files.

    Examples:
        rho-eval bird mini_dev_sqlite.json dev_databases/
        rho-eval bird mini_dev_sqlite.json dev_databases/ --difficulty challenging
        rho-eval bird mini_dev_sqlite.json dev_databases/ --no-evidence
    """
    # Validate difficulty
    if difficulty and difficulty not in ("simple", "moderate", "challenging"):
        console.print(f"[red]Invalid difficulty: {difficulty}[/red]")
        console.print("Valid values: simple, moderate, challenging")
        raise typer.Exit(1)

    # Load tasks
    console.print(f"Loading tasks from {data_file}...")
    tasks = load_bird_tasks(
        data_file,
        db_dir,
        include_evidence=not no_evidence,
        difficulty=difficulty,
    )
    console.print(f"Loaded {len(tasks)} tasks")

    if difficulty:
        console.print(f"Filtered to difficulty: {difficulty}")
    if no_evidence:
        console.print("Evidence hints withheld (hard mode)")

    # Apply offset and limit
    if offset > 0:
        tasks = tasks[offset:]
    if limit is not None:
        tasks = tasks[:limit]

    if not tasks:
        console.print("[yellow]No tasks to run.[/yellow]")
        raise typer.Exit(0)

    # Determine output directory
    if resume:
        run_dir = Path(resume)
        if not run_dir.exists():
            console.print(f"[red]Run directory not found: {resume}[/red]")
            raise typer.Exit(1)
        completed = get_completed_indices(run_dir)
        tasks = [t for t in tasks if t.index not in completed]
        console.print(f"Resuming: {len(completed)} done, {len(tasks)} remaining")
    else:
        if output is None:
            safe_model = model.replace("/", "-")
            base_path = Path("results") / f"{safe_model}-bird"
        else:
            base_path = Path(output)
        run_dir = create_run_dir(base_path)
        console.print(f"Results: {run_dir}")

    console.print(f"Running {len(tasks)} tasks (model={model})")

    # Create config
    config = EvalConfig(
        model=model,
        base_url=base_url,
        max_turns=max_turns,
        parallel=parallel,
        output_dir=str(run_dir),
        system_prompt_file=system_prompt,
        verbose=verbose,
        service_tier=service_tier,
    )

    # Save run config
    if not resume:
        save_run_config(
            {
                "model": model,
                "base_url": base_url,
                "max_turns": max_turns,
                "parallel": parallel,
                "data_file": data_file,
                "db_dir": db_dir,
                "difficulty": difficulty,
                "no_evidence": no_evidence,
                "system_prompt": system_prompt,
                "offset": offset,
                "limit": limit,
                "service_tier": service_tier,
            },
            run_dir,
        )

    # Run
    runner = BirdRunner(config)

    # When resuming, seed metrics with previously completed results so
    # incremental summary.txt updates reflect the full run.
    initial_metrics = rebuild_metrics_from_runs(run_dir) if resume else None

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("({task.completed}/{task.total})"),
        console=console,
    ) as progress:
        task_id = progress.add_task("Running BIRD-Bench...", total=len(tasks))

        def update_progress(completed: int, total: int) -> None:
            progress.update(task_id, completed=completed)

        try:
            results, metrics = asyncio.run(
                runner.run_tasks(
                    tasks,
                    output_dir=run_dir,
                    progress_callback=update_progress,
                    initial_metrics=initial_metrics,
                )
            )
        except EvalAbortedError as e:
            console.print()
            console.print(f"[red bold]Evaluation aborted:[/red bold] {e}")
            console.print(f"\nPartial results saved to: {run_dir}")
            raise typer.Exit(1)

    # Print summary
    console.print()
    console.print(format_summary(metrics))
    console.print(f"\nResults saved to: {run_dir}")
