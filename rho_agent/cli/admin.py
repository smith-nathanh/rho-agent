"""Admin commands: ps, cancel."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer

from ..core.session_store import SessionStore
from .theme import THEME
from .formatting import _format_elapsed, _markup
from .state import app, console


@app.command()
def ps(
    dir: Annotated[
        str,
        typer.Argument(help="Sessions directory to list"),
    ],
) -> None:
    """List sessions in a session directory."""
    sessions_dir = Path(dir).expanduser().resolve()
    if not sessions_dir.is_dir():
        console.print(_markup(f"Not a directory: {sessions_dir}", THEME.error))
        raise typer.Exit(1)

    store = SessionStore(sessions_dir)
    sessions = store.list(limit=50)
    if not sessions:
        console.print("[dim]No sessions found[/dim]")
        raise typer.Exit(0)

    now = datetime.now(timezone.utc)
    for info in sessions:
        try:
            started = datetime.fromisoformat(info.created_at)
            elapsed = _format_elapsed(started, now)
        except (ValueError, TypeError):
            elapsed = "?"

        status_color = {
            "running": THEME.success,
            "completed": THEME.muted,
            "error": THEME.error,
            "cancelled": THEME.warning,
        }.get(info.status, THEME.muted)

        preview = info.display_preview or ""
        console.print(
            f"  {_markup(info.id, THEME.accent)}  {_markup(info.status, status_color)}  "
            f"{_markup(f'{info.model:<14}', THEME.muted)}  {elapsed:>6}  {preview}"
        )


@app.command()
def cancel(
    prefix: Annotated[
        str | None,
        typer.Argument(help="Session ID prefix to cancel"),
    ] = None,
    dir: Annotated[
        str | None,
        typer.Option("--dir", "-d", help="Sessions directory"),
    ] = None,
    all: Annotated[
        bool,
        typer.Option("--all", help="Cancel all sessions"),
    ] = False,
) -> None:
    """Cancel running sessions by touching the cancel sentinel."""
    if not dir:
        console.print(_markup("Provide --dir <sessions_directory>", THEME.error))
        raise typer.Exit(1)

    sessions_dir = Path(dir).expanduser().resolve()
    if not sessions_dir.is_dir():
        console.print(_markup(f"Not a directory: {sessions_dir}", THEME.error))
        raise typer.Exit(1)

    if not all and not prefix:
        console.print(_markup("Provide a session ID prefix, or use --all", THEME.error))
        raise typer.Exit(1)

    cancelled_count = 0
    for child in sessions_dir.iterdir():
        if not child.is_dir():
            continue
        if all or child.name.startswith(prefix or ""):
            sentinel = child / "cancel"
            if not sentinel.exists():
                sentinel.touch()
                console.print(_markup(f"Cancelled: {child.name}", THEME.warning))
                cancelled_count += 1

    if cancelled_count:
        console.print(_markup(f"Sent cancel signal to {cancelled_count} sessions", THEME.success))
    else:
        console.print("[dim]No matching sessions to cancel[/dim]")
