"""Monitor command: interactive command center for telemetry and agent controls."""

import json
import shlex
from datetime import datetime, timezone
from time import monotonic, sleep
from typing import Annotated, Any, Optional

import typer
from rich.panel import Panel
from rich.table import Table

from ..command_center.services.control_plane import ControlPlane
from ..command_center.services.local_signal_transport import LocalSignalTransport
from ..observability.config import DEFAULT_TELEMETRY_DB
from ..observability.storage.sqlite import TelemetryStorage
from ..signals import SignalManager
from ..ui.theme import THEME
from .formatting import (
    _format_elapsed,
    _format_tool_preview,
    _format_token_count,
    _markup,
)
from .state import app, console, settings


class MonitorSession:
    """Encapsulates the state and command loop for the monitor command."""

    def __init__(
        self,
        storage: TelemetryStorage,
        sm: SignalManager,
        control_plane: ControlPlane,
        resolved_db: str,
        limit: int,
        read_write: bool,
    ) -> None:
        self.storage = storage
        self.sm = sm
        self.control_plane = control_plane
        self.resolved_db = resolved_db
        self.limit = limit
        self.read_write = read_write
        self.connection_state: dict[str, Any] | None = None

    def run(self) -> None:
        console.print(
            Panel(
                f"[bold]{_markup('rho-agent monitor', THEME.primary)}[/bold]\n"
                f"Database: {_markup(self.resolved_db, THEME.muted)}\n"
                f"Mode: {_markup('read-write' if self.read_write else 'read-only', THEME.muted)}\n"
                "Type [bold]help[/bold] for commands.",
                border_style=THEME.border,
            )
        )
        self._render_overview()

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
            if raw in ("overview", "o", "refresh", "r"):
                self._render_overview()
                continue
            if raw in ("running", "ps"):
                self._render_running()
                continue

            try:
                parts = shlex.split(raw)
            except ValueError as exc:
                console.print(_markup(f"Invalid command syntax: {exc}", THEME.error))
                continue
            command = parts[0].lower()

            if command == "sessions":
                status = None
                if len(parts) > 1 and parts[1] != "all":
                    status = parts[1]
                self._render_sessions(status=status)
                continue

            if command == "show" and len(parts) > 1:
                self._show_detail(parts[1])
                continue

            if command == "watch" and len(parts) > 1:
                self._watch_session(parts[1])
                continue

            if command in ("kill", "pause", "resume") and len(parts) > 1:
                target = parts[1]
                if command == "kill":
                    outcome = self.control_plane.kill(target)
                elif command == "pause":
                    outcome = self.control_plane.pause(target)
                else:
                    outcome = self.control_plane.resume(target)

                if outcome.acted_session_ids:
                    for sid in outcome.acted_session_ids:
                        console.print(_markup(f"{command}: {sid[:8]}", THEME.success))
                elif outcome.error:
                    console.print(_markup(outcome.error, THEME.error))
                elif outcome.warning:
                    console.print(_markup(outcome.warning, THEME.warning))
                else:
                    console.print(_markup(f"No agents updated by '{command}'", THEME.warning))
                continue

            if command == "directive" and len(parts) > 2:
                target = parts[1]
                directive = " ".join(parts[2:])
                outcome = self.control_plane.directive(target, directive)
                if outcome.acted_session_ids:
                    session_id = outcome.acted_session_ids[0]
                    console.print(_markup(f"directive queued for {session_id[:8]}", THEME.success))
                elif outcome.warning:
                    console.print(_markup(outcome.warning, THEME.warning))
                elif outcome.error:
                    console.print(_markup(outcome.error, THEME.error))
                else:
                    console.print(_markup("Failed to queue directive", THEME.error))
                continue

            if command == "connect":
                separator_index = parts.index("--") if "--" in parts else -1
                if separator_index == -1:
                    console.print(
                        _markup(
                            "Usage: connect <a_prefix> <b_prefix> [more_prefixes...] -- <task>",
                            THEME.warning,
                        )
                    )
                    continue
                left = parts[1:separator_index]
                if len(left) < 2:
                    console.print(
                        _markup(
                            "connect requires at least two agent prefixes before '--'.",
                            THEME.warning,
                        )
                    )
                    continue
                prefixes = left
                task = " ".join(parts[separator_index + 1 :]).strip()
                if not task:
                    console.print(_markup("connect task cannot be empty.", THEME.warning))
                    continue
                self._run_connect(prefixes, task)
                continue

            if command == "disconnect":
                if self.connection_state is None:
                    console.print(_markup("No active connect session.", THEME.warning))
                    continue

                session_ids = self.connection_state.get("session_ids", [])
                if not isinstance(session_ids, list):
                    session_ids = []

                for sid in session_ids:
                    self.sm.queue_directive(
                        sid,
                        "The connect session has ended. Resume your previous work.",
                    )
                    self.sm.clear_export(sid)

                self.connection_state = None
                console.print(_markup("Disconnected active connect session.", THEME.success))
                continue

            console.print(_markup(f"Unknown command: {raw}", THEME.warning))
            console.print("[dim]Type 'help' for command list[/dim]")

    def _print_help(self) -> None:
        console.print(_markup("Commands:", THEME.secondary))
        console.print("[dim]  overview                                running agents + active sessions[/dim]")
        console.print("[dim]  running                                 list running agents[/dim]")
        console.print(r"[dim]  sessions \[active|completed|all]        browse telemetry sessions (default: all)[/dim]")
        console.print("[dim]  show <id_or_prefix>                     session detail[/dim]")
        console.print("[dim]  watch <id_or_prefix>                    stream new tools + responses[/dim]")
        console.print("[dim]  kill <prefix|all>                       cancel running session(s)[/dim]")
        console.print("[dim]  pause <prefix|all>                      pause running session(s)[/dim]")
        console.print("[dim]  resume <prefix|all>                     resume paused session(s)[/dim]")
        console.print("[dim]  directive <prefix> <text>               inject directive into interactive run[/dim]")
        console.print(r"[dim]  connect <a> <b> \[more...] -- <task>   context-file collaboration[/dim]")
        console.print("[dim]  disconnect                              end active connect session[/dim]")
        console.print("[dim]  help                                    show this help[/dim]")
        console.print("[dim]  quit                                    exit monitor[/dim]")

    def _render_running(self) -> None:
        agents = self.sm.list_running()
        if not agents:
            console.print("[dim]No running agents[/dim]")
            return
        table = Table(show_header=True, header_style=THEME.secondary)
        table.add_column("Session", style=THEME.accent)
        table.add_column("State")
        table.add_column("Model", style=THEME.muted)
        table.add_column("Uptime", justify="right")
        table.add_column("Preview", overflow="fold")
        now = datetime.now(timezone.utc)
        for info in agents:
            paused = self.sm.is_paused(info.session_id)
            state = "paused" if paused else "running"
            state_color = THEME.warning if paused else THEME.success
            try:
                started = datetime.fromisoformat(info.started_at)
            except ValueError:
                started = now
            table.add_row(
                info.session_id[:8],
                _markup(state, state_color),
                info.model,
                _format_elapsed(started, now),
                info.instruction_preview,
            )
        console.print(table)

    def _render_sessions(self, status: str | None = None) -> None:
        sessions = self.storage.list_sessions(status=status, limit=self.limit)
        if not sessions:
            console.print("[dim]No telemetry sessions found[/dim]")
            return
        table = Table(show_header=True, header_style=THEME.secondary)
        table.add_column("Session", style=THEME.accent)
        table.add_column("Status")
        table.add_column("Model", style=THEME.muted)
        table.add_column("Team/Project", style=THEME.muted)
        table.add_column("Tokens", justify="right")
        table.add_column("Tools", justify="right")
        table.add_column("Turns", justify="right")
        table.add_column("Duration", justify="right")
        for s in sessions:
            total_tokens = s.total_input_tokens + s.total_output_tokens
            table.add_row(
                s.session_id[:8],
                s.status,
                s.model,
                f"{s.team_id}/{s.project_id}",
                _format_token_count(total_tokens),
                str(s.total_tool_calls),
                str(s.turn_count),
                _format_elapsed(s.started_at, s.ended_at),
            )
        console.print(table)

    def _resolve_single_running(self, prefix: str) -> str | None:
        session_id, error = self.control_plane.resolve_single_running(prefix)
        if not error:
            return session_id
        color = THEME.warning if "multiple sessions" in error else THEME.error
        console.print(_markup(error, color))
        return None

    def _resolve_session_id(self, prefix: str) -> str | None:
        detail = self.storage.get_session_detail(prefix)
        if detail:
            return prefix
        recent = self.storage.list_sessions(limit=200)
        matches = [s.session_id for s in recent if s.session_id.startswith(prefix)]
        if len(matches) == 1:
            return matches[0]
        return None

    def _resolve_watch_session_id(self, prefix: str) -> str | None:
        telemetry_match = self._resolve_session_id(prefix)
        if telemetry_match:
            return telemetry_match
        running_match = self._resolve_single_running(prefix)
        if running_match:
            return running_match
        return None

    @staticmethod
    def _truncate_for_directive(text: str, max_chars: int = 3000) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n...[truncated]..."

    def _wait_for_new_response(
        self,
        session_id: str,
        *,
        after_seq: int,
        timeout_seconds: int = 120,
    ) -> tuple[int, str] | None:
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            latest = self.sm.get_last_response(session_id)
            if latest and latest[0] > after_seq:
                return latest
            sleep(0.5)
        return None

    def _request_fresh_export(self, session_id: str) -> bool:
        self.sm.clear_export(session_id)
        return self.sm.request_export(session_id)

    def _wait_for_export(
        self,
        session_id: str,
        *,
        timeout_seconds: int = 60,
    ) -> bool:
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            if self.sm.export_ready(session_id):
                return True
            sleep(0.5)
        return False

    def _clear_connect_exports(self, session_ids: list[str]) -> None:
        for sid in session_ids:
            self.sm.clear_export(sid)

    def _build_connect_directive(
        self,
        *,
        session_id: str,
        session_ids: list[str],
        task: str,
        prior_responses: list[tuple[str, str]],
    ) -> str:
        peer_lines = [
            f"- {peer[:8]}: {self.sm.context_path(peer)}" for peer in session_ids if peer != session_id
        ]
        if peer_lines:
            peer_contexts = "\n".join(peer_lines)
        else:
            peer_contexts = "- (none)"

        if prior_responses:
            prior_text = "\n\n".join(
                f"[{peer[:8]}]\n{self._truncate_for_directive(text)}" for peer, text in prior_responses
            )
        else:
            prior_text = "(none yet)"

        return (
            f"Task: {task}\n"
            "This is a connect collaboration turn.\n"
            "Use read/grep tools on peer context files, then respond with your analysis.\n\n"
            "Peer context files:\n"
            f"{peer_contexts}\n\n"
            "Prior agent responses in this connect session:\n"
            f"{prior_text}"
        )

    def _run_connect(self, prefixes: list[str], task: str) -> None:
        if self.connection_state is not None:
            console.print(
                _markup(
                    "A connect session is already active. Run 'disconnect' before starting another.",
                    THEME.warning,
                )
            )
            return

        session_ids: list[str] = []
        for prefix in prefixes:
            resolved = self._resolve_single_running(prefix)
            if not resolved:
                return
            if resolved in session_ids:
                console.print(_markup(f"Duplicate agent prefix '{prefix}'", THEME.warning))
                continue
            session_ids.append(resolved)

        if len(session_ids) < 2:
            console.print(_markup("connect requires at least two distinct running agents.", THEME.error))
            return

        paused = [sid[:8] for sid in session_ids if self.sm.is_paused(sid)]
        if paused:
            console.print(
                _markup(
                    f"Paused agents detected ({', '.join(paused)}). Resume before connect.",
                    THEME.warning,
                )
            )
            return

        seq_by_session: dict[str, int] = {}
        for sid in session_ids:
            prior = self.sm.get_last_response(sid)
            seq_by_session[sid] = prior[0] if prior else 0

        for sid in session_ids:
            if not self._request_fresh_export(sid):
                console.print(_markup(f"Failed to request export for {sid[:8]}", THEME.error))
                self._clear_connect_exports(session_ids)
                return

        for sid in session_ids:
            if not self._wait_for_export(sid):
                console.print(
                    _markup(
                        f"Timed out waiting for context export from {sid[:8]}",
                        THEME.error,
                    )
                )
                self._clear_connect_exports(session_ids)
                return

        responses: list[tuple[str, str]] = []

        console.print(
            _markup(
                f"Starting connect with {len(session_ids)} agents",
                THEME.success,
            )
        )

        for sid in session_ids:
            prompt = self._build_connect_directive(
                session_id=sid,
                session_ids=session_ids,
                task=task,
                prior_responses=responses,
            )
            if not self.sm.queue_directive(sid, prompt):
                console.print(_markup(f"Failed to queue directive for {sid[:8]}", THEME.error))
                self._clear_connect_exports(session_ids)
                return

            latest = self._wait_for_new_response(sid, after_seq=seq_by_session[sid])
            if not latest:
                console.print(
                    _markup(
                        f"Timed out waiting for response from {sid[:8]}",
                        THEME.warning,
                    )
                )
                self._clear_connect_exports(session_ids)
                return

            seq_by_session[sid], response_text = latest
            responses.append((sid, response_text))

            if not self._request_fresh_export(sid) or not self._wait_for_export(sid, timeout_seconds=15):
                console.print(
                    _markup(
                        f"Failed to refresh context export for {sid[:8]} after response.",
                        THEME.warning,
                    )
                )
                self._clear_connect_exports(session_ids)
                return

            console.print(_markup(f"{sid[:8]}: response captured", THEME.muted))

        self.connection_state = {
            "session_ids": session_ids,
            "task": task,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        console.print(
            _markup(
                f"Connect session open for {len(session_ids)} agents. Use 'disconnect' to end it.",
                THEME.success,
            )
        )

    @staticmethod
    def _format_tool_args_preview(arguments: object, max_chars: int = 400) -> str:
        if not isinstance(arguments, dict):
            return "{}"
        text = json.dumps(arguments, ensure_ascii=False)
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "... [truncated]"

    def _watch_session(self, prefix: str, poll_interval_seconds: float = 1.0) -> None:
        session_id = self._resolve_watch_session_id(prefix)
        if not session_id:
            console.print(_markup(f"Session not found for prefix '{prefix}'", THEME.error))
            return

        detail = self.storage.get_session_detail(session_id)
        seen_turn_ids: set[str] = set()
        seen_execution_ids: set[str] = set()
        if detail:
            for turn in detail.turns:
                turn_id = turn.get("turn_id")
                if isinstance(turn_id, str):
                    seen_turn_ids.add(turn_id)
                for tool in turn.get("tool_executions", []):
                    execution_id = tool.get("execution_id")
                    if isinstance(execution_id, str):
                        seen_execution_ids.add(execution_id)

        latest_response = self.sm.get_last_response(session_id)
        last_response_seq = latest_response[0] if latest_response else 0

        console.print(
            _markup(
                f"Watching {session_id[:8]} (Ctrl+C to stop)",
                THEME.success,
            )
        )
        console.print(
            _markup(
                "Streams completed tool calls from telemetry and latest assistant responses.",
                THEME.muted,
            )
        )

        waiting_for_telemetry_announced = False
        seen_running = any(a.session_id == session_id for a in self.sm.list_running())

        try:
            while True:
                running = any(a.session_id == session_id for a in self.sm.list_running())
                seen_running = seen_running or running

                detail = self.storage.get_session_detail(session_id)
                if detail:
                    waiting_for_telemetry_announced = False
                    for turn in detail.turns:
                        turn_id = turn.get("turn_id")
                        if not isinstance(turn_id, str):
                            continue

                        if turn_id not in seen_turn_ids:
                            seen_turn_ids.add(turn_id)
                            user_input = str(turn.get("user_input") or "").strip()
                            if user_input:
                                preview = user_input.replace("\n", " ")
                                if len(preview) > 180:
                                    preview = preview[:180] + "..."
                                console.print(
                                    _markup(
                                        f"user: {preview}",
                                        THEME.secondary,
                                    )
                                )
                            else:
                                console.print(_markup("user: (empty)", THEME.secondary))

                        for tool in turn.get("tool_executions", []):
                            execution_id = tool.get("execution_id")
                            if not isinstance(execution_id, str) or execution_id in seen_execution_ids:
                                continue
                            seen_execution_ids.add(execution_id)

                            tool_name = str(tool.get("tool_name") or "tool")
                            success = bool(tool.get("success"))
                            duration_ms = int(tool.get("duration_ms") or 0)
                            status_text = "ok" if success else "error"
                            status_color = THEME.success if success else THEME.error
                            console.print(
                                f"{_markup(f'tool: {tool_name}', THEME.tool_call)} "
                                f"{_markup(status_text, status_color)} "
                                f"{_markup(f'({duration_ms} ms)', THEME.muted)}"
                            )

                            args_preview = self._format_tool_args_preview(tool.get("arguments"))
                            console.print(_markup(f"args: {args_preview}", THEME.muted))

                            if not success and tool.get("error"):
                                console.print(_markup(f"error: {tool['error']}", THEME.error))

                            result_preview = _format_tool_preview(
                                str(tool.get("result") or ""),
                                tool_name,
                                max_lines=settings.tool_preview_lines,
                            )
                            if result_preview:
                                console.print(_markup(result_preview, THEME.tool_result))
                            console.print()
                elif not waiting_for_telemetry_announced:
                    console.print(
                        _markup(
                            "Waiting for telemetry rows for this session...",
                            THEME.muted,
                        )
                    )
                    waiting_for_telemetry_announced = True

                latest_response = self.sm.get_last_response(session_id)
                if latest_response and latest_response[0] > last_response_seq:
                    last_response_seq = latest_response[0]
                    response_text = latest_response[1].strip()
                    response_preview = response_text
                    if len(response_preview) > 500:
                        response_preview = response_preview[:500] + "... [truncated]"
                    console.print(_markup("[assistant response]", THEME.primary))
                    console.print(_markup(response_preview, THEME.primary))
                    console.print()

                if detail and detail.status != "active" and not running:
                    console.print(_markup(f"Session ended ({detail.status}).", THEME.muted))
                    return

                if not running and seen_running and detail is None:
                    console.print(_markup("Session no longer running.", THEME.muted))
                    return

                sleep(poll_interval_seconds)
        except KeyboardInterrupt:
            console.print()
            console.print(_markup("Stopped watching.", THEME.muted))

    def _show_detail(self, prefix: str) -> None:
        session_id = self._resolve_session_id(prefix)
        if not session_id:
            console.print(_markup(f"Session not found for prefix '{prefix}'", THEME.error))
            return
        detail = self.storage.get_session_detail(session_id)
        if not detail:
            console.print(_markup(f"Session not found: {session_id}", THEME.error))
            return

        running = next((a for a in self.sm.list_running() if a.session_id == detail.session_id), None)
        paused = self.sm.is_paused(detail.session_id)
        status = "paused" if paused and detail.status == "active" else detail.status
        if status == "active":
            status_color = THEME.success
        elif status == "paused":
            status_color = THEME.warning
        else:
            status_color = THEME.warning

        console.print(
            Panel(
                f"Session: {_markup(detail.session_id, THEME.accent)}\n"
                f"Status: {_markup(status, status_color)}\n"
                f"Model: {_markup(detail.model, THEME.accent)}\n"
                f"Team/Project: {_markup(f'{detail.team_id}/{detail.project_id}', THEME.muted)}\n"
                f"Profile: {_markup(detail.profile or '-', THEME.muted)}\n"
                f"Duration: {_markup(_format_elapsed(detail.started_at, detail.ended_at), THEME.muted)}\n"
                f"Tokens: {_markup(_format_token_count(detail.total_input_tokens + detail.total_output_tokens), THEME.muted)}\n"
                f"Tool calls: {_markup(str(detail.total_tool_calls), THEME.muted)}\n"
                f"Turns: {_markup(str(len(detail.turns)), THEME.muted)}\n"
                f"PID: {_markup(str(running.pid), THEME.muted) if running else _markup('-', THEME.muted)}",
                border_style=THEME.border,
            )
        )

        if not detail.turns:
            console.print("[dim]No turns recorded[/dim]")
            return

        table = Table(show_header=True, header_style=THEME.secondary)
        table.add_column("Turn")
        table.add_column("Input", justify="right")
        table.add_column("Output", justify="right")
        table.add_column("Tools", justify="right")
        table.add_column("User Input Preview", overflow="fold")
        for turn in detail.turns[-10:]:
            preview = (turn.get("user_input") or "").replace("\n", " ")
            if len(preview) > 100:
                preview = preview[:100] + "..."
            table.add_row(
                str(turn.get("turn_index", "?")),
                _format_token_count(int(turn.get("input_tokens", 0))),
                _format_token_count(int(turn.get("output_tokens", 0))),
                str(len(turn.get("tool_executions", []))),
                preview,
            )
        console.print(table)

    def _render_overview(self) -> None:
        console.print(_markup("Running agents", THEME.secondary))
        self._render_running()
        console.print()
        console.print(_markup("Active sessions", THEME.secondary))
        self._render_sessions(status="active")
        completed_count = self.storage.count_sessions(status="completed")
        if completed_count:
            console.print(
                f"[dim]{completed_count} completed session{'s' if completed_count != 1 else ''}"
                " — type [bold]sessions[/bold] to browse[/dim]"
            )


@app.command()
def monitor(
    db_path: Annotated[
        Optional[str],
        typer.Option("--db", help="Path to telemetry database"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Number of sessions to list in overview"),
    ] = 20,
    read_write: Annotated[
        bool,
        typer.Option(
            "--read-write",
            help="Open telemetry DB in read-write mode (default is read-only)",
        ),
    ] = False,
) -> None:
    """Interactive command center for telemetry and live agent controls."""
    resolved_db = db_path or str(DEFAULT_TELEMETRY_DB)
    sm = SignalManager()
    control_plane = ControlPlane(LocalSignalTransport(sm))
    try:
        storage = TelemetryStorage(resolved_db, read_only=not read_write)
    except Exception as exc:
        console.print(_markup(f"Failed to open telemetry DB: {exc}", THEME.error))
        raise typer.Exit(1) from exc

    session = MonitorSession(
        storage=storage,
        sm=sm,
        control_plane=control_plane,
        resolved_db=resolved_db,
        limit=limit,
        read_write=read_write,
    )
    session.run()
