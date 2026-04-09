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
    ] = "score_child_prop",
    meta_timeout: Annotated[
        int,
        typer.Option("--meta-timeout", help="Meta-agent wall-clock timeout in seconds"),
    ] = 3600,
    transfer_from: Annotated[
        str | None,
        typer.Option(
            "--transfer-from",
            help="Path to a previous run dir; seeds from its best generation",
        ),
    ] = None,
) -> None:
    """Run an evolutionary loop to build and improve task-agents."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if transfer_from and seed:
        raise typer.BadParameter("--transfer-from and --seed are mutually exclusive")

    harness_kwargs: dict[str, str] = {}
    if harness_arg:
        for arg in harness_arg:
            if "=" not in arg:
                raise typer.BadParameter(f"Expected key=value format: {arg}")
            key, value = arg.split("=", 1)
            harness_kwargs[key] = value

    resolved_dir = str(Path(run_dir).expanduser().resolve())

    # Cross-run transfer: materialize best generation from source run as seed
    resolved_seed = seed
    resolved_transfer = None
    if transfer_from:
        from .archive import best_generation as _best_gen, load_archive as _load_archive
        from .workspace import materialize_workspace as _materialize

        source_dir = Path(transfer_from).expanduser().resolve()
        source_archive_path = source_dir / "archive.jsonl"
        if not source_archive_path.exists():
            raise typer.BadParameter(f"No archive.jsonl found in {source_dir}")
        source_archive = _load_archive(source_archive_path)
        best = _best_gen(source_archive_path)
        if best is None:
            raise typer.BadParameter(f"No scored generations in {source_dir}")
        typer.echo(
            f"Transferring from {best.gen_id} (score={best.score:.4f}) in {source_dir}"
        )
        # Re-materialize from diff chain in case workspace was cleaned up
        transfer_ws = _materialize(
            str(source_dir),
            f"_transfer_{best.gen_id}",
            source_archive,
            parent_id=best.gen_id,
        )
        resolved_seed = str(transfer_ws)
        resolved_transfer = str(source_dir)

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
        seed_workspace=resolved_seed,
        staged_sample_n=staged_sample,
        harness_kwargs=harness_kwargs,
        daytona_backend=daytona_backend,
        parent_strategy=parent_strategy,
        meta_timeout=meta_timeout,
        transfer_from=resolved_transfer,
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


def evolve_eval(
    harness: Annotated[
        str,
        typer.Argument(help="Dotted path to DomainHarness subclass"),
    ],
    workspace: Annotated[
        str,
        typer.Argument(help="Path to workspace directory to evaluate"),
    ],
    task_model: Annotated[
        str,
        typer.Option("--task-model", help="Task-agent model"),
    ] = DEFAULT_MODEL,
    split: Annotated[
        str,
        typer.Option("--split", help="Dataset split to evaluate on: train, val, or test"),
    ] = "test",
    harness_arg: Annotated[
        list[str] | None,
        typer.Option("--harness-arg", help="key=value pairs passed to harness constructor"),
    ] = None,
) -> None:
    """Evaluate a workspace against a harness split (default: test set)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    harness_kwargs: dict[str, str] = {}
    if harness_arg:
        for arg in harness_arg:
            if "=" not in arg:
                raise typer.BadParameter(f"Expected key=value format: {arg}")
            key, value = arg.split("=", 1)
            harness_kwargs[key] = value

    from .harness import load_harness
    from .models import EvolveConfig
    from .workspace import build_agent_from_workspace

    ws_path = Path(workspace).expanduser().resolve()
    if not ws_path.exists():
        raise typer.BadParameter(f"Workspace not found: {ws_path}")

    h = load_harness(harness, **harness_kwargs)

    # Build a minimal config just for the task model
    config = EvolveConfig(harness=harness, task_model=task_model)

    async def _run() -> None:
        await h.ensure_loaded()

        if split == "train":
            scenarios = h.scenarios()
        elif split == "val":
            scenarios = h.staged_sample(len(h.scenarios()))  # full val set
        elif split == "test":
            if hasattr(h, "test_scenarios"):
                scenarios = h.test_scenarios()
            elif hasattr(h, "_splits"):
                scenarios = h._splits["test"]  # type: ignore[attr-defined]
            else:
                raise typer.BadParameter("Harness does not expose a test split")
        else:
            raise typer.BadParameter(f"Unknown split: {split}")

        agent = build_agent_from_workspace(ws_path, config)
        h.set_workspace(ws_path, config)

        typer.echo(f"Evaluating {ws_path.name} on {split} split ({len(scenarios)} scenarios)...")

        results = await h.run_all(agent, scenarios)
        for i, result in enumerate(results):
            sid = result.get("scenario_id", "?")
            status = "correct" if result.get("success") else "wrong"
            typer.echo(f"  [{i+1}/{len(scenarios)}] {sid}: {status}")

        score = h.score(results)
        feedback = h.feedback(results)
        typer.echo(f"\nScore: {score:.4f}")
        typer.echo(f"\n{feedback}")

    asyncio.run(_run())
