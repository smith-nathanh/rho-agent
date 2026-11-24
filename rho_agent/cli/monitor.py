"""Monitor command: interactive command shell and live Mission Control dashboard."""

from __future__ import annotations

import builtins
import shlex
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Annotated

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich import box
from rich.console import Group, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from ..core.approvals import ApprovalRequest, pending_requests, respond
from ..core.session_meta import LIVE_STATUSES, effective_status, read_meta
from ..core.session_store import SessionInfo, SessionStore
from .completion import MonitorCompleter
from .formatting import (
    _format_cost,
    _format_elapsed,
    _format_tool_signature,
    _is_interactive_terminal,
    _markup,
    _shorten_path,
)
from .monitor_live import MissionControl, _tail_trace_lines, render_trace_event, write_directive
from .session_table import _project_label, fleet_counts, print_sessions
from .state import CONFIG_DIR, MONITOR_HISTORY_FILE, SESSIONS_DIR, app, console
from .theme import PTK_STYLE, THEME, THEMES, set_theme, status_style
from .ui import render_approval_card, render_themes_card

_BUILTIN_INPUT = builtins.input


class MonitorSession:
    """Interactive command shell and live dashboard for session directory observability."""

    def __init__(self, sessions_dir: Path) -> None:
        self._dir = sessions_dir
        self._store = SessionStore(sessions_dir)
        # Short-TTL snapshot shared between _prompt_tokens and _toolbar_tokens
        self._snap_cache: (
            tuple[float, list[SessionInfo], list[tuple[SessionInfo, ApprovalRequest]]] | None
        ) = None

    def _cached_snapshot(
        self, ttl: float = 0.8
    ) -> tuple[list[SessionInfo], list[tuple[SessionInfo, ApprovalRequest]]]:
        now = monotonic()
        if self._snap_cache is not None and (now - self._snap_cache[0]) < ttl:
            return self._snap_cache[1], self._snap_cache[2]
        sessions = self._store.list(limit=50)
        pending = self._store.pending_approvals()
        self._snap_cache = (now, sessions, pending)
        return sessions, pending

    def _use_ptk(self) -> bool:
        """Use prompt_toolkit in real interactive TTYs; fall back to input() when mocked/piped."""
        return _is_interactive_terminal() and builtins.input is _BUILTIN_INPUT

    def _prompt_tokens(self) -> list[tuple[str, str]]:
        """Minimal dynamic prompt with subtle live counters."""
        parts: list[tuple[str, str]] = [
            ("class:prompt.mark", "ρ "),
            ("class:prompt.monitor", "monitor"),
        ]
        try:
            sessions, pending = self._cached_snapshot()
            counts, _, _ = fleet_counts(sessions)
            pending_n = len(pending)
            badges: list[tuple[str, str]] = []
            if counts["running"]:
                badges.append(("class:prompt.badge.live", f"●{counts['running']}"))
            if pending_n:
                badges.append(("class:prompt.badge.alert", f"●{pending_n}"))
            if badges:
                parts.append(("class:prompt.muted", " "))
                for idx, badge in enumerate(badges):
                    if idx:
                        parts.append(("class:prompt.muted", " "))
                    parts.append(badge)
        except Exception:
            pass
        parts.append(("class:prompt.arrow", " › "))
        return parts

    def _toolbar_tokens(self) -> list[tuple[str, str]]:
        """Quiet auto-refreshing bottom status bar for the monitor shell."""
        parts: list[tuple[str, str]] = [("class:toolbar.muted", " ")]
        try:
            sessions, pending = self._cached_snapshot()
            counts, total_cost, _ = fleet_counts(sessions)
            pending_n = len(pending)

            if counts["running"]:
                parts.append(("class:toolbar.accent", f"● {counts['running']} running"))
            else:
                parts.append(("class:toolbar.muted", "○ 0 running"))

            if pending_n:
                parts.append(("class:toolbar.sep", "  ·  "))
                parts.append(("class:toolbar.warn", "● "))
                parts.append(
                    ("class:toolbar.ok", f"{pending_n} approval{'s' if pending_n != 1 else ''}")
                )

            if counts["waiting_input"]:
                parts.append(("class:toolbar.sep", "  ·  "))
                parts.append(("class:toolbar.muted", f"{counts['waiting_input']} idle"))

            if counts["paused"]:
                parts.append(("class:toolbar.sep", "  ·  "))
                parts.append(("class:toolbar.muted", f"{counts['paused']} paused"))

            cost_str = _format_cost(total_cost)
            if cost_str:
                parts.append(("class:toolbar.sep", "  ·  "))
                parts.append(("class:toolbar.ok", cost_str))
        except Exception:
            parts.append(("class:toolbar.muted", "monitor"))

        parts.append(("class:toolbar.sep", "  ·  "))
        parts.append(("class:toolbar.muted", "live  ·  Tab complete  ·  help  ·  exit "))
        return parts

    def _print_banner(self) -> None:
        top = Table.grid(expand=True)
        top.add_column(ratio=1)
        top.add_column(justify="right", no_wrap=True)

        title = Text()
        title.append("ρ ", style=f"bold {THEME.mark}")
        title.append("rho-agent monitor", style=f"bold {THEME.brand}")
        title.append(f"  {_shorten_path(str(self._dir))}", style=THEME.muted)
        hints = Text("live · ps · watch · review · help · exit", style=THEME.muted)
        top.add_row(title, hints)

        console.print(Group(Rule(style=THEME.border), top, Rule(style=THEME.border)))

    def run(self, *, start_live: bool = False) -> None:
        """Start the interactive monitor command loop (or live Mission Control)."""
        if start_live:
            result = MissionControl(self._store).run(on_watch=self._cmd_watch)
            if result != "shell":
                return

        self._print_banner()
        self._cmd_ps()

        prompt_session: PromptSession[str] | None = None
        if self._use_ptk():
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            prompt_session = PromptSession(
                history=FileHistory(str(MONITOR_HISTORY_FILE)),
                completer=MonitorCompleter(self._store),
                bottom_toolbar=self._toolbar_tokens,
                refresh_interval=1.0,
                style=PTK_STYLE,
                complete_while_typing=False,
            )

        while True:
            try:
                if prompt_session is not None:
                    raw = prompt_session.prompt(self._prompt_tokens).strip()
                else:
                    raw = input("monitor> ").strip()
            except EOFError:
                console.print()
                break
            except KeyboardInterrupt:
                console.print()
                continue

            if not raw:
                continue
            if raw in ("quit", "exit", "q", "/exit", "/quit"):
                break
            if raw in ("help", "h", "?"):
                self._print_help()
                continue
            if raw in ("clear", "cls"):
                console.clear()
                continue

            try:
                parts = shlex.split(raw)
            except ValueError as exc:
                console.print(_markup(f"Invalid command syntax: {exc}", THEME.error))
                continue

            command = parts[0].lower()

            if command == "ps":
                self._cmd_ps()
            elif command in ("live", "top", "dash", "dashboard"):
                exit_mode = MissionControl(self._store).run(on_watch=self._cmd_watch)
                if exit_mode == "quit":
                    break
                self._cmd_ps()
            elif command == "watch" and len(parts) > 1:
                self._cmd_watch(parts[1], parts[2] if len(parts) > 2 else None)
            elif command == "inspect" and len(parts) > 1:
                self._cmd_inspect(parts[1])
            elif command == "approvals":
                self._cmd_approvals()
            elif command == "review":
                self._cmd_review()
            elif command in ("approve", "deny") and len(parts) > 1:
                self._cmd_answer(parts[1], approved=command == "approve")
            elif command == "cancel" and len(parts) > 1:
                self._cmd_cancel(parts[1])
            elif command == "pause" and len(parts) > 1:
                self._cmd_pause(parts[1])
            elif command == "resume" and len(parts) > 1:
                self._cmd_resume(parts[1])
            elif command == "directive" and len(parts) > 2:
                self._cmd_directive(parts[1], " ".join(parts[2:]))
            elif command == "theme":
                self._cmd_theme(parts[1] if len(parts) > 1 else None)
            else:
                console.print(_markup(f"Unknown command: {raw}", THEME.warning))
                console.print(f"[{THEME.muted}]Type 'help' for command list[/{THEME.muted}]")

    def _print_help(self) -> None:
        sections: list[tuple[str, list[tuple[str, str]]]] = [
            (
                "Observe",
                [
                    ("ps", "List sessions grouped by project"),
                    ("live  (top / dash)", "Open live auto-refreshing Mission Control"),
                    ("watch <id> [n|all]", "Follow a session trace live (default: last 20)"),
                    ("inspect <id>", "Show metadata, subagents & recent events"),
                ],
            ),
            (
                "Approvals",
                [
                    ("approvals", "List pending tool approval requests"),
                    ("review", "Step through pending approvals interactively"),
                    ("approve <req|all>", "Approve one request or all pending"),
                    ("deny <req>", "Deny a pending request"),
                ],
            ),
            (
                "Control",
                [
                    ("directive <id> <text>", "Inject an instruction before the next step"),
                    ("pause <id|all>", "Pause live session(s)"),
                    ("resume <id|all>", "Resume paused session(s)"),
                    ("cancel <id|all>", "Cancel live session(s)"),
                    ("theme [name]", "List or switch CLI color themes"),
                    ("clear / help / exit", "Clear screen, show help, or exit"),
                ],
            ),
        ]

        blocks: list[RenderableType] = []
        for idx, (heading, rows) in enumerate(sections):
            if idx:
                blocks.append(Text(""))
            blocks.append(Text(heading, style=f"bold {THEME.primary}"))
            grid = Table.grid(padding=(0, 3))
            grid.add_column(width=24, style=THEME.secondary, no_wrap=True)
            grid.add_column(style=THEME.muted)
            for lhs, rhs in rows:
                grid.add_row(f"  {lhs}", rhs)
            blocks.append(grid)

        console.print(
            Panel(
                Group(*blocks),
                title=f"[{THEME.secondary}]Monitor Commands[/{THEME.secondary}]",
                title_align="left",
                box=box.SQUARE,
                border_style=THEME.border,
                padding=(0, 1),
            )
        )

    def _resolve_dirs(self, prefix: str) -> list[Path]:
        """Resolve prefix to matching session directories."""
        return [info.path for info in self._store.find(prefix) if info.path is not None]

    def _resolve_single_dir(self, prefix: str) -> Path | None:
        """Resolve prefix to exactly one session directory."""
        matches = self._resolve_dirs(prefix)
        if not matches:
            console.print(_markup(f"No session matching '{prefix}'", THEME.error))
            return None
        if len(matches) > 1:
            names = ", ".join(m.name for m in matches[:5])
            console.print(_markup(f"Ambiguous prefix '{prefix}': {names}", THEME.warning))
            return None
        return matches[0]

    def _cmd_ps(self) -> None:
        sessions = self._store.list(limit=50)
        pending = self._store.pending_approvals()
        print_sessions(sessions, pending_approvals=len(pending))
        if pending:
            lines: list[Text] = []
            for info, req in pending[:5]:
                sig = _format_tool_signature(req.tool_name, req.tool_args)
                row = Text()
                row.append(f"  {req.id}", style=f"bold {THEME.brand}")
                row.append(f"  #{info.id}  ", style=THEME.muted)
                row.append(sig, style=THEME.primary)
                lines.append(row)
            if len(pending) > 5:
                lines.append(Text(f"  … and {len(pending) - 5} more", style=THEME.muted))
            lines.append(
                Text("  review · approve <id> · approve all · deny <id>", style=THEME.muted)
            )
            console.print(
                Panel(
                    Group(*lines),
                    title=(
                        f"[{THEME.warning}]●[/{THEME.warning}] "
                        f"[bold {THEME.brand}]{len(pending)} Approval"
                        f"{'s' if len(pending) != 1 else ''} Waiting[/bold {THEME.brand}]"
                    ),
                    title_align="left",
                    box=box.SQUARE,
                    border_style=THEME.border_active,
                    padding=(0, 1),
                )
            )

    def _cmd_inspect(self, prefix: str) -> None:
        """Show detailed metadata, children, pending approvals, and recent trace for one session."""
        matches = self._store.find(prefix)
        if not matches:
            console.print(_markup(f"No session matching '{prefix}'", THEME.error))
            return
        if len(matches) > 1:
            names = ", ".join(m.id for m in matches[:5])
            console.print(_markup(f"Ambiguous prefix '{prefix}': {names}", THEME.warning))
            return

        info = matches[0]
        glyph, color = status_style(info.status)
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=THEME.muted, no_wrap=True)
        grid.add_column()

        st = Text()
        st.append(f"{glyph} ", style=color)
        st.append(info.status, style=THEME.primary)
        if info.pid:
            st.append(f"  ·  pid {info.pid}", style=THEME.muted)
        if info.user or info.host:
            owner = f"{info.user}@{info.host}" if info.host else info.user
            st.append(f"  ·  {owner}", style=THEME.muted)
        grid.add_row("status", st)
        grid.add_row("model", Text(info.model or "?", style=THEME.secondary))
        if info.access:
            grid.add_row("access", Text(info.access, style=THEME.subtle))
        if info.cwd:
            grid.add_row("project", Text(_project_label(info.cwd), style=THEME.muted))
        if info.cost_usd:
            grid.add_row("cost", Text(_format_cost(info.cost_usd), style=THEME.primary))
        if info.first_prompt:
            grid.add_row("prompt", Text(info.display_preview, style=THEME.primary))

        blocks: list[RenderableType] = [grid]

        if info.children:
            blocks.append(Text(""))
            blocks.append(Text("Subagents", style=THEME.muted))
            for idx, child in enumerate(info.children):
                conn = "├─ " if idx < len(info.children) - 1 else "└─ "
                cg, cc = status_style(child.status)
                row = Text("  ")
                row.append(conn, style=THEME.border)
                row.append(f"{child.id}  ", style=THEME.primary)
                row.append(f"{cg} {child.status}  ", style=cc)
                row.append(child.display_preview, style=THEME.muted)
                blocks.append(row)

        if info.path is not None:
            reqs = pending_requests(info.path)
            if reqs:
                blocks.append(Text(""))
                blocks.append(Text("Pending approvals", style=THEME.secondary))
                for req in reqs:
                    sig = _format_tool_signature(req.tool_name, req.tool_args)
                    row = Text("  ")
                    row.append("● ", style=THEME.warning)
                    row.append(f"{req.id}  ", style=f"bold {THEME.brand}")
                    row.append(sig, style=THEME.primary)
                    blocks.append(row)

        recent = _tail_trace_lines(info.path, limit=14)
        rendered = [
            r
            for line in recent
            if (r := render_trace_event(line, compact=False, working_dir=info.cwd or None))
            is not None
        ]
        if rendered:
            blocks.append(Text(""))
            blocks.append(Text("Recent activity", style=THEME.muted))
            blocks.extend(rendered[-8:])

        console.print(
            Panel(
                Group(*blocks),
                title=f"[{THEME.primary}]Session #{escape(info.id)}[/{THEME.primary}]",
                title_align="left",
                box=box.SQUARE,
                border_style=THEME.border,
                padding=(0, 1),
            )
        )

    def _cmd_approvals(self) -> None:
        pending = self._store.pending_approvals()
        if not pending:
            console.print(f"[{THEME.muted}]No pending approvals[/{THEME.muted}]")
            return
        table = Table(
            show_header=True,
            header_style=THEME.muted,
            box=box.SIMPLE_HEAD,
            border_style=THEME.border,
            expand=True,
            pad_edge=False,
        )
        table.add_column("Request", style=f"bold {THEME.primary}", no_wrap=True)
        table.add_column("Session", style=THEME.primary, no_wrap=True)
        table.add_column("Waiting", justify="right", style=THEME.muted, no_wrap=True)
        table.add_column("Tool call", overflow="fold", style=THEME.subtle)
        now = datetime.now(UTC)
        for info, request in pending:
            try:
                waiting = _format_elapsed(datetime.fromisoformat(request.requested_at), now)
            except (ValueError, TypeError):
                waiting = "?"
            signature = _format_tool_signature(request.tool_name, request.tool_args)
            table.add_row(
                Text(request.id, style=THEME.warning),
                Text(info.id, style=THEME.primary),
                Text(waiting, style=THEME.muted),
                Text(signature, style=THEME.subtle),
            )
        console.print(table)
        console.print(
            f"[{THEME.muted}]review · approve <id> · approve all · deny <id>[/{THEME.muted}]"
        )

    def _cmd_review(self) -> None:
        """Interactively step through pending approval requests one by one."""
        pending = self._store.pending_approvals()
        if not pending:
            console.print(f"[{THEME.muted}]No pending approvals to review.[/{THEME.muted}]")
            return
        total = len(pending)
        for idx, (info, req) in enumerate(pending, start=1):
            console.print(f"\n[{THEME.muted}]Request {idx}/{total} · #{info.id}[/{THEME.muted}]")
            panel, _ = render_approval_card(
                req.tool_name,
                req.tool_args,
                request_id=req.id,
                working_dir=info.cwd or None,
            )
            console.print(panel)
            console.print(
                f"  {_markup('[y] approve  [n] deny  [s] skip  [q] stop:', THEME.warning)} ",
                end="",
            )
            try:
                ans = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if ans in ("y", "yes"):
                respond(req, True)
                console.print(
                    _markup(f"  approved: {req.id} ({req.tool_name}, {info.id})", THEME.secondary)
                )
            elif ans in ("n", "no"):
                respond(req, False)
                console.print(
                    _markup(f"  denied: {req.id} ({req.tool_name}, {info.id})", THEME.warning)
                )
            elif ans in ("q", "quit"):
                break
            else:
                console.print(_markup(f"  skipped {req.id}", THEME.muted))

    def _cmd_answer(self, prefix: str, *, approved: bool) -> None:
        pending = self._store.pending_approvals()
        if prefix == "all" and approved:
            matches = pending
        else:
            matches = [pair for pair in pending if pair[1].id.startswith(prefix)]
        if not matches:
            console.print(_markup(f"No pending approval matching '{prefix}'", THEME.error))
            return
        if len(matches) > 1 and prefix != "all":
            ids = ", ".join(request.id for _, request in matches[:5])
            console.print(_markup(f"Ambiguous request '{prefix}': {ids}", THEME.warning))
            return
        for info, request in matches:
            respond(request, approved)
            verb, color = ("approved", THEME.secondary) if approved else ("denied", THEME.warning)
            console.print(_markup(f"{verb}: {request.id} ({request.tool_name}, {info.id})", color))

    def _cmd_watch(self, prefix: str, tail: str | None = None) -> None:
        """Follow a session's trace: the last events first, then new ones as they appear."""
        if tail is None:
            skip_to_last: int | None = 20
        elif tail == "all":
            skip_to_last = None
        elif tail.isdigit():
            skip_to_last = int(tail)
        else:
            console.print(_markup("Usage: watch <prefix> [n|all]", THEME.warning))
            return
        session_dir = self._resolve_single_dir(prefix)
        if not session_dir:
            return

        trace_path = session_dir / "trace.jsonl"
        if not trace_path.exists():
            console.print(_markup("No trace.jsonl found", THEME.error))
            return

        meta = read_meta(session_dir)
        status = effective_status(meta) if meta else "unknown"
        glyph, color = status_style(status)
        cwd = str(meta.get("cwd") or "") or None

        hdr = Text()
        hdr.append("watch ", style=THEME.muted)
        hdr.append(f"#{session_dir.name}", style=f"bold {THEME.primary}")
        hdr.append("  ·  ", style=THEME.border_active)
        hdr.append(f"{glyph} {status}", style=color)
        if meta.get("model"):
            hdr.append(f"  ·  {meta['model']}", style=THEME.muted)
        if cwd:
            hdr.append(f"  ·  {_project_label(cwd)}", style=THEME.muted)
        hdr.append("   Ctrl+C to stop", style=THEME.muted)
        console.print(Group(Rule(style=THEME.border), hdr, Rule(style=THEME.border)))

        try:
            with open(trace_path, encoding="utf-8") as f:
                if skip_to_last is not None:
                    past = [line for line in f if line.strip()]
                    if len(past) > skip_to_last:
                        console.print(
                            _markup(
                                f"  … {len(past) - skip_to_last} earlier events "
                                f"(watch {session_dir.name} all)",
                                THEME.muted,
                            )
                        )
                    for line in past[len(past) - skip_to_last :] if skip_to_last else []:
                        self._render_trace_event(line, working_dir=cwd)
                while True:
                    line = f.readline()
                    if line:
                        line = line.strip()
                        if line:
                            self._render_trace_event(line, working_dir=cwd)
                    else:
                        meta = read_meta(session_dir)
                        if meta:
                            status = effective_status(meta)
                            if status not in LIVE_STATUSES:
                                sg, sc = status_style(status)
                                console.print(_markup(f"  {sg} Session ended ({status})", sc))
                                return
                        sleep(0.5)
        except KeyboardInterrupt:
            console.print()
            console.print(_markup("Stopped watching.", THEME.muted))

    def _render_trace_event(self, line: str, *, working_dir: str | None = None) -> None:
        """Render a single trace.jsonl line."""
        item = render_trace_event(line, compact=False, working_dir=working_dir)
        if item is not None:
            console.print(item)

    def _cmd_cancel(self, prefix: str) -> None:
        matches = self._store.find(prefix)
        if not matches:
            console.print(_markup(f"No sessions matching '{prefix}'", THEME.error))
            return
        for info in matches:
            if self._store.request_cancel(info):
                console.print(_markup(f"cancel: {info.id}", THEME.warning))
            elif prefix != "all":
                console.print(_markup(f"{info.id}: not running ({info.status})", THEME.muted))

    def _cmd_pause(self, prefix: str) -> None:
        matches = self._store.find(prefix)
        if not matches:
            console.print(_markup(f"No sessions matching '{prefix}'", THEME.error))
            return
        for info in matches:
            if info.live and info.path is not None:
                (info.path / "pause").touch()
                console.print(_markup(f"pause: {info.id}", THEME.secondary))
            elif prefix != "all":
                console.print(_markup(f"{info.id}: not running ({info.status})", THEME.muted))

    def _cmd_resume(self, prefix: str) -> None:
        dirs = self._resolve_dirs(prefix)
        if not dirs:
            console.print(_markup(f"No sessions matching '{prefix}'", THEME.error))
            return
        for d in dirs:
            pause_path = d / "pause"
            if pause_path.exists():
                pause_path.unlink()
                console.print(_markup(f"resume: {d.name}", THEME.secondary))
            else:
                console.print(_markup(f"{d.name}: not paused", THEME.muted))

    def _cmd_theme(self, name: str | None) -> None:
        if not name:
            console.print(render_themes_card(command_prefix="theme"))
            return
        try:
            chosen = set_theme(name, persist=True)
        except ValueError as exc:
            console.print(_markup(str(exc), THEME.error))
            return
        console.print(
            _markup(f"✓ Theme set to {chosen.name} ({chosen.description})", THEME.success)
        )

    def _cmd_directive(self, prefix: str, text: str) -> None:
        session_dir = self._resolve_single_dir(prefix)
        if not session_dir:
            return
        ok, msg, status = write_directive(session_dir, text)
        if not ok:
            console.print(_markup(msg, THEME.error))
            return
        console.print(_markup(msg, THEME.secondary))
        if status not in LIVE_STATUSES:
            console.print(
                _markup(
                    f"{session_dir.name} is not running ({status}). It will read this when "
                    f"it is resumed: rho-agent -r {session_dir.name}",
                    THEME.warning,
                )
            )


@app.command()
def monitor(
    dir: Annotated[
        str | None,
        typer.Argument(help="Sessions directory to monitor (default: the CLI's sessions)"),
    ] = None,
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            "-L",
            help="Launch straight into the live auto-refreshing Mission Control dashboard",
        ),
    ] = False,
    theme: Annotated[
        str | None,
        typer.Option(
            "--theme",
            help=f"Color theme override ({', '.join(THEMES)})",
        ),
    ] = None,
) -> None:
    """Interactive command shell and live Mission Control dashboard for sessions."""
    if theme:
        try:
            set_theme(theme)
        except ValueError as exc:
            console.print(_markup(str(exc), THEME.error))
            raise typer.Exit(1) from exc

    sessions_dir = Path(dir).expanduser().resolve() if dir else SESSIONS_DIR
    if not sessions_dir.is_dir():
        console.print(_markup(f"Not a directory: {sessions_dir}", THEME.error))
        raise typer.Exit(1)

    session = MonitorSession(sessions_dir)
    session.run(start_live=live)
