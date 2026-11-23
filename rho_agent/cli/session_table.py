"""Session listing and fleet summary shared by ``rho-agent ps`` and ``rho-agent monitor``."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from rich import box
from rich.table import Table
from rich.text import Text

from ..core.session_store import SessionInfo
from .formatting import _format_cost, _format_elapsed
from .state import console
from .theme import THEME, status_style


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


def _walk_sessions(sessions: Iterable[SessionInfo]) -> Iterable[SessionInfo]:
    for info in sessions:
        yield info
        yield from _walk_sessions(info.children)


def _access_short(access: str) -> Text:
    if not access:
        return Text("", style=THEME.muted)
    if access == "read-only":
        return Text("ro", style=THEME.muted)
    if access == "full":
        return Text("full", style=THEME.warning)
    if access == "workspace":
        return Text("ws", style=THEME.subtle)
    return Text(access, style=THEME.muted)


def fleet_counts(sessions: list[SessionInfo]) -> tuple[Counter[str], float, int]:
    """Return ``(status_counts, total_cost_usd, total_sessions)`` across the fleet."""
    counts: Counter[str] = Counter()
    total_cost = 0.0
    total = 0
    for info in sessions:
        total_cost += info.cost_usd
        for node in _walk_sessions([info]):
            counts[node.status] += 1
            total += 1
    return counts, total_cost, total


def build_fleet_summary(
    sessions: list[SessionInfo],
    *,
    pending_approvals: int = 0,
) -> Text:
    """Quiet architectural summary strip for active sessions, alerts, and spend."""
    counts, total_cost, total = fleet_counts(sessions)
    bar = Text()
    bar.append("ρ ", style=f"bold {THEME.mark}")
    bar.append("fleet", style=f"bold {THEME.brand}")
    bar.append("  ", style="")

    items: list[tuple[str, str, str, str]] = []
    if counts["running"]:
        items.append(("●", f"{counts['running']} running", THEME.accent, THEME.secondary))
    if pending_approvals or counts["waiting_approval"]:
        n = max(pending_approvals, counts["waiting_approval"])
        items.append(("●", f"{n} approval{'s' if n != 1 else ''}", THEME.warning, THEME.primary))
    if counts["waiting_input"]:
        items.append(("○", f"{counts['waiting_input']} idle", THEME.secondary, THEME.secondary))
    if counts["paused"]:
        items.append(("▪", f"{counts['paused']} paused", THEME.muted, THEME.muted))
    if counts["completed"]:
        items.append(("·", f"{counts['completed']} done", THEME.muted, THEME.muted))
    failed = counts["error"] + counts["dead"]
    if failed:
        items.append(("×", f"{failed} error", THEME.error, THEME.error))

    if not items:
        bar.append("no active sessions", style=THEME.muted)
    else:
        for idx, (glyph, label, dot_color, label_color) in enumerate(items):
            if idx:
                bar.append("  ·  ", style=THEME.border)
            bar.append(f"{glyph} ", style=dot_color)
            bar.append(label, style=label_color)

    cost_str = _format_cost(total_cost)
    if cost_str:
        bar.append("  │  ", style=THEME.border)
        bar.append(cost_str, style=THEME.primary)

    bar.append(f"  ({total})", style=THEME.muted)
    return bar


def print_sessions(
    sessions: list[SessionInfo],
    *,
    show_summary: bool = True,
    pending_approvals: int = 0,
) -> None:
    """Print fleet summary + one minimal hairline table per project, with child trees."""
    if not sessions:
        console.print(f"[{THEME.muted}]No sessions found[/{THEME.muted}]")
        return

    if show_summary:
        console.print(build_fleet_summary(sessions, pending_approvals=pending_approvals))

    owners = {(s.user, s.host) for s in sessions if s.user or s.host}
    show_owner = len(owners) > 1
    wide = console.width >= 100

    def new_table(title: str) -> Table:
        table = Table(
            title=Text(title, style=f"bold {THEME.primary}"),
            title_justify="left",
            show_header=True,
            header_style=THEME.muted,
            box=box.SIMPLE_HEAD,
            border_style=THEME.border,
            expand=True,
            pad_edge=False,
        )
        table.add_column("Session", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        if wide:
            table.add_column("Access", no_wrap=True)
        table.add_column("Model", style=THEME.muted, no_wrap=True)
        if show_owner:
            table.add_column("Owner", style=THEME.muted, no_wrap=True)
        if wide:
            table.add_column("Started", justify="right", style=THEME.muted, no_wrap=True)
        table.add_column("Active", justify="right", style=THEME.muted, no_wrap=True)
        table.add_column("Cost", justify="right", style=THEME.secondary, no_wrap=True)
        table.add_column("Preview", no_wrap=True, overflow="ellipsis", ratio=1)
        return table

    now = datetime.now(UTC)

    def add(table: Table, info: SessionInfo, prefix_parts: list[bool]) -> None:
        id_cell = Text()
        if prefix_parts:
            for has_more in prefix_parts[:-1]:
                id_cell.append("│  " if has_more else "   ", style=THEME.border)
            connector = "├─ " if prefix_parts[-1] else "└─ "
            id_cell.append(connector, style=THEME.border)
            id_cell.append(info.id, style=THEME.secondary)
        else:
            id_cell.append(info.id, style=f"bold {THEME.primary}" if info.live else THEME.subtle)

        glyph, color = status_style(info.status)
        status_cell = Text()
        status_cell.append(f"{glyph} ", style=color)
        status_cell.append(
            info.status,
            style=color
            if info.status in ("error", "dead")
            else (
                THEME.primary if info.status in ("running", "waiting_approval") else THEME.subtle
            ),
        )

        row: list[object] = [id_cell, status_cell]
        if wide:
            row.append(_access_short(info.access))
        row.append(Text(info.model, style=THEME.muted))
        if show_owner:
            owner = f"{info.user}@{info.host}" if info.host else info.user
            row.append(Text(owner, style=THEME.muted))
        if wide:
            row.append(_ago(info.created_at, now))
        row += [
            _ago(info.last_activity, now),
            _format_cost(info.cost_usd),
            Text(
                info.display_preview,
                style=THEME.subtle if info.live else THEME.muted,
            ),
        ]
        table.add_row(*row)
        child_count = len(info.children)
        for idx, child in enumerate(info.children):
            add(table, child, [*prefix_parts, idx < child_count - 1])

    by_project: dict[str, list[SessionInfo]] = {}
    for info in sessions:
        by_project.setdefault(info.cwd, []).append(info)

    for cwd, group in by_project.items():
        table = new_table(_project_label(cwd))
        for info in group:
            add(table, info, [])
        console.print(table)
