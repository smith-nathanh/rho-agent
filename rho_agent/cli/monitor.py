"""Monitor command: interactive command loop operating on session directories."""

from __future__ import annotations

import fcntl
import json
import shlex
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from typing import Annotated

import typer
from rich.panel import Panel
from rich.table import Table

from ..core.session_store import SessionStore
from .theme import THEME
from .formatting import (
    _format_elapsed,
    _format_token_count,
    _markup,
)
from .state import app, console


class MonitorSession:
    """Interactive command loop for session directory observability."""

    def __init__(self, sessions_dir: Path) -> None:
        self._dir = sessions_dir
        self._store = SessionStore(sessions_dir)

    def run(self) -> None:
        """Start the interactive monitor command loop."""
        console.print(
            Panel(
                f"[bold]{_markup('rho-agent monitor', THEME.primary)}[/bold]\n"
                f"Directory: {_markup(str(self._dir), THEME.muted)}\n"
                "Type [bold]help[/bold] for commands.",
                border_style=THEME.border,
            )
        )
        self._cmd_ps()

        while True:
            try:
                raw = input("monitor> ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break

            if not raw:
                continue
            if raw in ("quit", "exit", "q"):
                break
            if raw in ("help", "h", "?"):
                self._print_help()
                continue

            try:
                parts = shlex.split(raw)
            except ValueError as exc:
                console.print(_markup(f"Invalid command syntax: {exc}", THEME.error))
                continue

            command = parts[0].lower()

            if command == "ps":
                self._cmd_ps()
            elif command == "watch" and len(parts) > 1:
                self._cmd_watch(parts[1])
            elif command == "cancel" and len(parts) > 1:
                self._cmd_cancel(parts[1])
            elif command == "pause" and len(parts) > 1:
                self._cmd_pause(parts[1])
            elif command == "resume" and len(parts) > 1:
                self._cmd_resume(parts[1])
            elif command == "directive" and len(parts) > 2:
                self._cmd_directive(parts[1], " ".join(parts[2:]))
            else:
                console.print(_markup(f"Unknown command: {raw}", THEME.warning))
                console.print("[dim]Type 'help' for command list[/dim]")

    def _print_help(self) -> None:
        console.print(_markup("Commands:", THEME.secondary))
        console.print("[dim]  ps                                      list sessions[/dim]")
        console.print("[dim]  watch <prefix>                          tail trace events[/dim]")
        console.print("[dim]  cancel <prefix|all>                    cancel session(s)[/dim]")
        console.print("[dim]  pause <prefix|all>                     pause session(s)[/dim]")
        console.print("[dim]  resume <prefix|all>                    resume session(s)[/dim]")
        console.print("[dim]  directive <prefix> <text>               inject directive[/dim]")
        console.print("[dim]  help                                    show this help[/dim]")
        console.print("[dim]  quit                                    exit monitor[/dim]")

    def _resolve_dirs(self, prefix: str) -> list[Path]:
        """Resolve prefix to matching session directories."""
        if prefix == "all":
            return [d for d in self._dir.iterdir() if d.is_dir()]
        return [d for d in self._dir.iterdir() if d.is_dir() and d.name.startswith(prefix)]

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
        if not sessions:
            console.print("[dim]No sessions found[/dim]")
            return

        table = Table(show_header=True, header_style=THEME.secondary)
        table.add_column("Session", style=THEME.accent)
        table.add_column("Status")
        table.add_column("Model", style=THEME.muted)
        table.add_column("Started", justify="right")
        table.add_column("Preview", overflow="fold")

        now = datetime.now(timezone.utc)
        for info in sessions:
            status_color = {
                "running": THEME.success,
                "completed": THEME.muted,
                "error": THEME.error,
                "cancelled": THEME.warning,
            }.get(info.status, THEME.muted)

            try:
                started = datetime.fromisoformat(info.created_at)
                elapsed = _format_elapsed(started, now)
            except (ValueError, TypeError):
                elapsed = "?"

            table.add_row(
                info.id,
                _markup(info.status, status_color),
                info.model,
                elapsed,
                info.display_preview,
            )
        console.print(table)

    def _cmd_watch(self, prefix: str) -> None:
        """Tail trace.jsonl for a session, rendering events as they appear."""
        session_dir = self._resolve_single_dir(prefix)
        if not session_dir:
            return

        trace_path = session_dir / "trace.jsonl"
        if not trace_path.exists():
            console.print(_markup("No trace.jsonl found", THEME.error))
            return

        console.print(_markup(f"Watching {session_dir.name} (Ctrl+C to stop)", THEME.success))

        try:
            with open(trace_path, encoding="utf-8") as f:
                while True:
                    line = f.readline()
                    if line:
                        line = line.strip()
                        if line:
                            self._render_trace_event(line)
                    else:
                        # Check if session is still running
                        meta_path = session_dir / "meta.json"
                        if meta_path.exists():
                            try:
                                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                                if meta.get("status") not in ("running",):
                                    console.print(
                                        _markup(
                                            f"Session ended ({meta.get('status', '?')})",
                                            THEME.muted,
                                        )
                                    )
                                    return
                            except (json.JSONDecodeError, OSError):
                                pass
                        sleep(0.5)
        except KeyboardInterrupt:
            console.print()
            console.print(_markup("Stopped watching.", THEME.muted))

    def _render_trace_event(self, line: str) -> None:
        """Render a single trace.jsonl line."""
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return

        ev_type = event.get("event", "")

        if ev_type == "run_start":
            prompt = event.get("prompt", "")
            preview = prompt[:120].replace("\n", " ")
            if len(prompt) > 120:
                preview += "..."
            console.print(_markup(f"run_start: {preview}", THEME.secondary))

        elif ev_type == "run_end":
            status = event.get("status", "?")
            console.print(_markup(f"run_end: {status}", THEME.muted))

        elif ev_type == "llm_start":
            model = event.get("model", "?")
            ctx = event.get("context_size", "?")
            console.print(_markup(f"llm_start model={model} context={ctx}", THEME.muted))

        elif ev_type == "llm_end":
            inp = event.get("input_tokens", 0)
            out = event.get("output_tokens", 0)
            cost = event.get("cost_usd", 0.0)
            console.print(
                _markup(
                    f"llm_end in={_format_token_count(inp)} out={_format_token_count(out)} cost=${cost:.4f}",
                    THEME.muted,
                )
            )

        elif ev_type == "tool_start":
            name = event.get("tool_name", "?")
            args = event.get("tool_args", {})
            args_str = json.dumps(args, ensure_ascii=False)
            if len(args_str) > 200:
                args_str = args_str[:200] + "..."
            console.print(_markup(f"tool: {name}({args_str})", THEME.tool_call))

        elif ev_type == "tool_end":
            name = event.get("tool_name", "?")
            ok = event.get("success", True)
            status_str = "ok" if ok else "error"
            color = THEME.success if ok else THEME.error
            console.print(f"  {_markup(f'{name}: {status_str}', color)}")

        elif ev_type == "tool_blocked":
            name = event.get("tool_name", "?")
            console.print(_markup(f"  {name}: BLOCKED", THEME.warning))

        elif ev_type == "message":
            role = event.get("role", "?")
            content = event.get("content", "")
            if role == "assistant" and content:
                preview = content[:200].replace("\n", " ")
                if len(content) > 200:
                    preview += "..."
                console.print(_markup(f"assistant: {preview}", THEME.primary))

        elif ev_type == "compact":
            before = event.get("tokens_before", "?")
            after = event.get("tokens_after", "?")
            console.print(_markup(f"compact: {before} -> {after} tokens", THEME.muted))

    def _cmd_cancel(self, prefix: str) -> None:
        dirs = self._resolve_dirs(prefix)
        if not dirs:
            console.print(_markup(f"No sessions matching '{prefix}'", THEME.error))
            return
        for d in dirs:
            (d / "cancel").touch()
            console.print(_markup(f"cancel: {d.name}", THEME.warning))

    def _cmd_pause(self, prefix: str) -> None:
        dirs = self._resolve_dirs(prefix)
        if not dirs:
            console.print(_markup(f"No sessions matching '{prefix}'", THEME.error))
            return
        for d in dirs:
            (d / "pause").touch()
            console.print(_markup(f"pause: {d.name}", THEME.success))

    def _cmd_resume(self, prefix: str) -> None:
        dirs = self._resolve_dirs(prefix)
        if not dirs:
            console.print(_markup(f"No sessions matching '{prefix}'", THEME.error))
            return
        for d in dirs:
            pause_path = d / "pause"
            if pause_path.exists():
                pause_path.unlink()
                console.print(_markup(f"resume: {d.name}", THEME.success))
            else:
                console.print(_markup(f"{d.name}: not paused", THEME.muted))

    def _cmd_directive(self, prefix: str, text: str) -> None:
        session_dir = self._resolve_single_dir(prefix)
        if not session_dir:
            return
        path = session_dir / "directives.jsonl"
        entry = json.dumps({"text": text}) + "\n"
        try:
            with open(path, "a", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.write(entry)
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            console.print(_markup(f"directive queued for {session_dir.name}", THEME.success))
        except OSError as exc:
            console.print(_markup(f"Failed to write directive: {exc}", THEME.error))


@app.command()
def monitor(
    dir: Annotated[
        str,
        typer.Argument(help="Sessions directory to monitor"),
    ],
) -> None:
    """Interactive command loop for session directory observability."""
    sessions_dir = Path(dir).expanduser().resolve()
    if not sessions_dir.is_dir():
        console.print(_markup(f"Not a directory: {sessions_dir}", THEME.error))
        raise typer.Exit(1)

    session = MonitorSession(sessions_dir)
    session.run()
