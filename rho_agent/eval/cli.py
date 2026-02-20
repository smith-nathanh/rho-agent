"""Unified CLI for all rho-agent evaluations."""

from __future__ import annotations

import typer

# Import birdbench command
from .birdbench.cli import bird

app = typer.Typer(
    name="rho-eval",
    help="Run evaluations through rho-agent.",
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Run evaluations through rho-agent."""


# Register commands
app.command(name="bird")(bird)

if __name__ == "__main__":
    app()
