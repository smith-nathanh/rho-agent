"""Unified CLI for all rho-agent evaluations.

Entry point: rho-eval

Commands:
  dbbench          - AgentBench DBBench evaluation
  os-interaction   - AgentBench OS Interaction evaluation
  bird             - BIRD-Bench text-to-SQL evaluation
"""

import typer

# Import existing agentbench commands
from .agentbench.cli import dbbench, os_interaction

# Import birdbench command
from .birdbench.cli import bird

app = typer.Typer(
    name="rho-eval",
    help="Run evaluations through rho-agent.",
    add_completion=False,
)

# Register commands
app.command()(dbbench)
app.command(name="os-interaction")(os_interaction)
app.command()(bird)

if __name__ == "__main__":
    app()
