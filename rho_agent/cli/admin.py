"""Admin commands: ps, cancel."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ..core.session_store import SessionStore
from .formatting import _markup
from .session_table import print_sessions
from .state import SESSIONS_DIR, app, console
from .theme import THEME


def _sessions_dir(dir: str | None) -> Path:
    """Resolve a sessions directory argument, defaulting to the CLI's own."""
    sessions_dir = Path(dir).expanduser().resolve() if dir else SESSIONS_DIR
    if not sessions_dir.is_dir():
        console.print(_markup(f"Not a directory: {sessions_dir}", THEME.error))
        raise typer.Exit(1)
    return sessions_dir


@app.command()
def ps(
    dir: Annotated[
        str | None,
        typer.Argument(help="Sessions directory to list (default: the CLI's sessions)"),
    ] = None,
) -> None:
    """List sessions by project, with status, cost and child sessions."""
    print_sessions(SessionStore(_sessions_dir(dir)).list(limit=50))


@app.command()
def cancel(
    prefix: Annotated[
        str | None,
        typer.Argument(help="Session ID prefix to cancel"),
    ] = None,
    dir: Annotated[
        str | None,
        typer.Option("--dir", "-d", help="Sessions directory (default: the CLI's sessions)"),
    ] = None,
    all: Annotated[
        bool,
        typer.Option("--all", help="Cancel all live sessions"),
    ] = False,
) -> None:
    """Cancel the current run of live sessions."""
    store = SessionStore(_sessions_dir(dir))

    if not all and not prefix:
        console.print(_markup("Provide a session ID prefix, or use --all", THEME.error))
        raise typer.Exit(1)

    cancelled_count = 0
    for info in store.find("all" if all else (prefix or "")):
        if store.request_cancel(info):
            console.print(_markup(f"Cancelled: {info.id}", THEME.warning))
            cancelled_count += 1
        elif not all:
            console.print(_markup(f"{info.id}: not running ({info.status})", THEME.muted))

    if cancelled_count:
        console.print(_markup(f"Sent cancel signal to {cancelled_count} sessions", THEME.success))
    else:
        console.print("[dim]No live sessions to cancel[/dim]")
