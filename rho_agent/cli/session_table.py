"""Session listing shared by ``rho-agent ps`` and the monitor's ``ps``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from rich.table import Table

from ..core.session_store import SessionInfo
from .formatting import _format_elapsed, _markup
from .state import console
from .theme import THEME

STATUS_COLORS = {
    "created": THEME.muted,
    "running": THEME.success,
    "waiting_input": THEME.accent,
    "waiting_approval": THEME.warning,
    "paused": THEME.warning,
    "completed": THEME.muted,
    "cancelled": THEME.warning,
    "error": THEME.error,
    "dead": THEME.error,
    "unknown": THEME.muted,
}


def _ago(iso: str, now: datetime) -> str:
    try:
        then = datetime.fromisoformat(iso)
        days = (now - then).days
        return f"{days}d" if days >= 2 else _format_elapsed(then, now)
    except (ValueError, TypeError):
        return "?"


def _project_label(cwd: str) -> str:
    if not cwd:
        return "(no project recorded)"
    try:
        return "~/" + str(Path(cwd).relative_to(Path.home()))
    except ValueError:
        return cwd


def print_sessions(sessions: list[SessionInfo]) -> None:
    """Print one table per project, with child sessions under their parent."""
    if not sessions:
        console.print("[dim]No sessions found[/dim]")
        return

    # The owner column only earns its space when a directory is shared.
    owners = {(s.user, s.host) for s in sessions if s.user or s.host}
    show_owner = len(owners) > 1

    def new_table(title: str) -> Table:
        table = Table(
            title=title,
            title_justify="left",
            title_style=f"bold {THEME.secondary}",
            show_header=True,
            header_style=THEME.secondary,
        )
        table.add_column("Session", style=THEME.accent, no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Model", style=THEME.muted)
        if show_owner:
            table.add_column("Owner", style=THEME.muted)
        table.add_column("Started", justify="right", no_wrap=True)
        table.add_column("Active", justify="right", no_wrap=True)
        table.add_column("Cost", justify="right", no_wrap=True)
        table.add_column("Preview", overflow="fold")
        return table

    now = datetime.now(UTC)

    def add(table: Table, info: SessionInfo, depth: int) -> None:
        label = info.id if depth == 0 else f"{'  ' * (depth - 1)}└ {info.id}"
        row = [
            label,
            _markup(info.status, STATUS_COLORS.get(info.status, THEME.muted)),
            info.model,
        ]
        if show_owner:
            row.append(f"{info.user}@{info.host}" if info.host else info.user)
        row += [
            _ago(info.created_at, now),
            _ago(info.last_activity, now),
            f"${info.cost_usd:.2f}" if info.cost_usd else "",
            info.display_preview,
        ]
        table.add_row(*row)
        for child in info.children:
            add(table, child, depth + 1)

    # Projects in order of their most recently active session.
    by_project: dict[str, list[SessionInfo]] = {}
    for info in sessions:
        by_project.setdefault(info.cwd, []).append(info)

    for cwd, group in by_project.items():
        table = new_table(_project_label(cwd))
        for info in group:
            add(table, info, 0)
        console.print(table)
