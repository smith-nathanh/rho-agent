"""CLI helpers for Harbor integration assets."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

app = typer.Typer(help="Harbor integration helpers.", no_args_is_help=True)
console = Console()


def _config_root():
    return files("rho_agent.eval.harbor").joinpath("configs")


def _available_configs() -> dict[str, object]:
    configs = {}
    for item in _config_root().iterdir():
        if item.name.endswith(".yaml"):
            configs[item.name] = item
            configs[item.name.removesuffix(".yaml")] = item
    return configs


@app.command("list-configs")
def list_configs() -> None:
    """List bundled Harbor config templates."""
    configs = sorted({name for name in _available_configs() if name.endswith(".yaml")})
    for name in configs:
        console.print(name)


@app.command("write-config")
def write_config(
    name: Annotated[
        str,
        typer.Argument(help="Config name or filename, e.g. terminal-bench-prelim"),
    ],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Where to write the config file"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite the output file if it exists"),
    ] = False,
) -> None:
    """Write a bundled Harbor config template to a local file."""
    configs = _available_configs()
    config = configs.get(name)
    if config is None:
        available = ", ".join(sorted({n for n in configs if n.endswith(".yaml")}))
        raise typer.BadParameter(f"unknown config {name!r}. Available: {available}")

    target = output or Path(config.name)
    if target.exists() and not force:
        raise typer.BadParameter(f"{target} already exists; use --force to overwrite")

    target.write_text(config.read_text(), encoding="utf-8")
    console.print(str(target))
