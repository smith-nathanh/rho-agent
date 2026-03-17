"""CLI package for rho-agent."""

from __future__ import annotations

import sys

# Import subcommand modules so their @app.command() decorators register
from . import admin as _admin  # noqa: F401
from . import export_cmd as _export_cmd  # noqa: F401
from . import main_cmd as _main_cmd  # noqa: F401
from . import monitor as _monitor  # noqa: F401
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
)
from .formatting import TokenStatus
from .interactive import run_interactive
from .main_cmd import main
from .single import run_single, run_single_with_output
from .state import app

_continuum_registered = False


def cli() -> None:
    """CLI entrypoint with `main` as the default command."""
    global _continuum_registered

    if not _continuum_registered:
        from ..continuum.cli import continuum as _continuum_fn

        app.command(name="continuum")(_continuum_fn)
        _continuum_registered = True

    args = sys.argv[1:]
    subcommands = {"main", "monitor", "ps", "cancel", "export", "continuum"}

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
    "run_interactive",
    "run_single",
    "run_single_with_output",
]


if __name__ == "__main__":
    cli()
