"""CLI package for rho-agent."""

import sys

from .errors import (
    CliUsageError,
    InvalidModeError,
    InvalidProfileError,
    MissingApiKeyError,
    PromptLoadError,
)
from .events import (
    ApprovalHandler,
    handle_command,
    handle_event,
    switch_runtime_profile,
)
from .formatting import TokenStatus
from .interactive import run_interactive
from .single import run_single, run_single_with_output
from .state import app

# Import subcommand modules so their @app.command() decorators register
from . import admin as _admin  # noqa: F401
from . import monitor as _monitor  # noqa: F401
from . import main_cmd as _main_cmd  # noqa: F401

# Re-export main for backwards compatibility
from .main_cmd import main


def cli() -> None:
    """CLI entrypoint with `main` as the default command."""
    # Register conductor subcommand (lazy import to avoid circular deps)
    from ..conductor.cli import conduct as _conduct_fn

    app.command(name="conduct")(_conduct_fn)

    args = sys.argv[1:]
    subcommands = {"main", "dashboard", "monitor", "ps", "kill", "conduct"}

    if not args or args[0] not in subcommands:
        args = ["main", *args]

    app(args=args, prog_name="rho-agent")


__all__ = [
    "ApprovalHandler",
    "CliUsageError",
    "InvalidModeError",
    "InvalidProfileError",
    "MissingApiKeyError",
    "PromptLoadError",
    "TokenStatus",
    "app",
    "cli",
    "handle_command",
    "handle_event",
    "main",
    "monitor",
    "run_interactive",
    "run_single",
    "run_single_with_output",
    "switch_runtime_profile",
]


if __name__ == "__main__":
    cli()
