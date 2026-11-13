"""Interactive REPL session."""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import signal
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

from ..core.events import AgentEvent
from ..core.session import Session
from ..core.session_meta import update_meta
from ..core.session_store import SessionStore
from .completion import create_completer
from .events import ApprovalHandler, handle_command, handle_event
from .formatting import (
    TokenStatus,
    _get_version,
    _is_interactive_terminal,
    _markup,
    _sync_token_status_from_state,
)
from .single import upload_to_sandbox
from .state import CONFIG_DIR, HISTORY_FILE, RENDER_MARKDOWN, console
from .theme import THEME


class InteractiveSession:
    """Encapsulates the mutable state and logic for an interactive REPL session."""

    def __init__(
        self,
        session: Session,
        approval_handler: ApprovalHandler,
        mode_name: str,
        working_dir: str,
        session_store: SessionStore,
        upload_mappings: list[tuple[str, str]] | None = None,
        setup_commands: list[str] | None = None,
    ) -> None:
        self.session = session
        self.approval_handler = approval_handler
        self.mode_name = mode_name
        self.working_dir = working_dir
        self.session_store = session_store
        self.upload_mappings = upload_mappings or []
        self.setup_commands = setup_commands or []
        self.session_status = "completed"
        self.token_status = TokenStatus(
            input_tokens=session.state.usage.get("input_tokens", 0),
            output_tokens=session.state.usage.get("output_tokens", 0),
        )
        self.interactive_tty = _is_interactive_terminal()

    async def run(self) -> None:
        """Run the interactive REPL loop until exit or error."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        key_bindings = KeyBindings()

        @key_bindings.add("enter")
        def _(event: Any) -> None:
            event.app.current_buffer.validate_and_handle()

        @key_bindings.add("escape", "enter")
        def _(event: Any) -> None:
            event.app.current_buffer.insert_text("\n")

        prompt_session: PromptSession[str] = PromptSession(
            history=FileHistory(str(HISTORY_FILE)),
            completer=create_completer(working_dir=self.working_dir),
            multiline=True,
            key_bindings=key_bindings,
            bottom_toolbar=(lambda: self.token_status.render()),
            complete_while_typing=False,
            complete_in_thread=True,
        )

        # Welcome message
        from rich.panel import Panel

        version = _get_version()
        console.print(
            Panel(
                f"[bold]{_markup('ρ rho-agent', THEME.primary)}[/bold] v{version}\n"
                f"Mode: {_markup(self.mode_name, THEME.accent)}\n"
                f"Model: {_markup(self.session.agent.config.model, THEME.accent)}\n"
                "Enter to send, Esc+Enter for newline, Ctrl+C to cancel.\n"
                "Type [bold]/help[/bold] for commands, [bold]exit[/bold] to quit.",
                border_style=THEME.border,
            )
        )

        try:
            # Upload files to sandbox before starting interactive loop
            if self.upload_mappings:
                await upload_to_sandbox(self.session, self.upload_mappings)

            # Run setup commands after upload, before interactive loop
            if self.setup_commands:
                from .single import run_setup_commands

                await run_setup_commands(
                    self.session, self.setup_commands, self.working_dir
                )

            while True:
                try:
                    console.print()
                    self.session.set_status("waiting_input")
                    source, user_input = await self._next_input(prompt_session)
                except (EOFError, KeyboardInterrupt):
                    break

                if source == "cancel":
                    self.session_status = "cancelled"
                    console.print(_markup("Cancelled by external signal", THEME.warning))
                    break
                if source == "directive" and not user_input:
                    continue
                if source == "directive":
                    console.print(_markup("Directive from monitor:", THEME.secondary))
                    console.print(user_input)
                    await self._execute_turn(user_input)
                    if self.session_status == "cancelled":
                        break
                    continue

                if not user_input:
                    continue

                if user_input.lower() in ("exit", "quit"):
                    break

                if user_input.startswith("/"):
                    if user_input.startswith("/download"):
                        await self._handle_download(user_input)
                        continue
                    if user_input.startswith("/write"):
                        self._handle_file_write_toggle(user_input)
                        continue
                    if user_input.startswith("/resume"):
                        self._handle_resume(user_input)
                        continue

                    action = handle_command(user_input, self.approval_handler)
                    if action and action.startswith("compact"):
                        instructions = ""
                        if ":" in action:
                            instructions = action.split(":", 1)[1]
                        handle_event(AgentEvent(type="compact_start", content="manual"))
                        result = await self.session.compact(
                            custom_instructions=instructions, trigger="manual"
                        )
                        handle_event(
                            AgentEvent(
                                type="compact_end",
                                content=f"Compacted: {result.tokens_before} -> {result.tokens_after} tokens",
                            )
                        )
                    continue

                await self._execute_turn(user_input)
                if self.session_status == "cancelled":
                    break
        except Exception:
            self.session_status = "error"
            raise
        finally:
            self.session.set_status(self.session_status)
            await self.session.close()
            if self.session.state.messages:
                console.print(f"\n[dim]Goodbye! Session saved: {self.session.id}[/dim]")
            else:
                console.print(_markup("\nGoodbye!", THEME.muted))

    def _external_request(self) -> str | None:
        """A request left by the monitor while idle: ``cancel``, ``directive`` or None."""
        session_dir = self.session._session_dir
        if session_dir is None:
            return None
        if (session_dir / "cancel").exists():
            return "cancel"
        try:
            if (session_dir / "directives.jsonl").stat().st_size > 0:
                return "directive"
        except OSError:
            pass
        return None

    async def _next_input(
        self, prompt_session: PromptSession[str], poll_seconds: float = 0.5
    ) -> tuple[str, str]:
        """Wait for the user to type something or for the monitor to step in.

        Returns:
            ``("user", text)`` for typed input, ``("directive", text)`` for
            directives sent from the monitor, or ``("cancel", "")``.

        Raises:
            EOFError, KeyboardInterrupt: The user closed the prompt.
        """

        async def watch() -> str:
            while True:
                request = self._external_request()
                if request:
                    return request
                await asyncio.sleep(poll_seconds)

        prompt_task = asyncio.ensure_future(
            prompt_session.prompt_async(HTML(f"<style fg='{THEME.prompt}'><b>&gt;</b></style> "))
        )
        watch_task = asyncio.ensure_future(watch())
        try:
            await asyncio.wait({prompt_task, watch_task}, return_when=asyncio.FIRST_COMPLETED)
            if prompt_task.done():
                return "user", prompt_task.result().strip()

            # The monitor stepped in: close the prompt cleanly, dropping any half-typed text.
            if prompt_session.app.is_running:
                prompt_session.app.exit(result="")
            else:
                prompt_task.cancel()
            await asyncio.gather(prompt_task, return_exceptions=True)

            if watch_task.result() == "cancel":
                self.session._clear_cancel_request()
                return "cancel", ""
            return "directive", "\n\n".join(self.session._consume_directives())
        finally:
            for task in (prompt_task, watch_task):
                if not task.done():
                    task.cancel()

    async def _execute_turn(self, user_input: str) -> None:
        """Run one agent turn, streaming events to the console."""
        loop = asyncio.get_event_loop()
        response_chunks: list[str] = []

        def on_cancel():
            console.print(f"\n{_markup('Cancelling...', THEME.warning)}")
            self.session.cancel()

        if platform.system() != "Windows":
            loop.add_signal_handler(signal.SIGINT, on_cancel)

        try:
            status_ctx = None
            start = monotonic()
            if self.interactive_tty:
                status_ctx = console.status(
                    "⠋ working (0s)",
                    spinner="dots",
                    spinner_style=THEME.accent,
                )
                status_ctx.__enter__()
            saw_model_output = False
            pending_text_chunks: list[str] = []

            async def on_event(event: AgentEvent) -> None:
                nonlocal saw_model_output, status_ctx, pending_text_chunks
                if status_ctx and not saw_model_output:
                    elapsed = int(monotonic() - start)
                    status_ctx.update(f"⠋ working ({elapsed}s • Ctrl+C: cancel)")
                if event.type == "text" and event.content:
                    response_chunks.append(event.content)
                if event.type in ("text", "tool_start", "error", "cancelled"):
                    saw_model_output = True
                    if status_ctx:
                        status_ctx.__exit__(None, None, None)
                        status_ctx = None
                if event.type == "cancelled":
                    _sync_token_status_from_state(self.token_status, self.session.state)
                    # Cancelled from another process (monitor or `rho-agent cancel`)
                    if self.session.cancelled_externally:
                        self.session_status = "cancelled"
                        console.print(_markup("Cancelled by external signal", THEME.warning))
                    else:
                        console.print(_markup("Turn cancelled", THEME.muted))
                    return
                if event.type == "error":
                    _sync_token_status_from_state(self.token_status, self.session.state)
                handle_event(
                    event,
                    show_turn_usage=False,
                    token_status=self.token_status,
                    render_markdown=self.interactive_tty and RENDER_MARKDOWN,
                    pending_text_chunks=pending_text_chunks,
                )

            await self.session.run(user_input, on_event=on_event)

            if status_ctx:
                status_ctx.__exit__(None, None, None)
        finally:
            if platform.system() != "Windows":
                loop.remove_signal_handler(signal.SIGINT)

    async def _handle_download(self, cmd: str) -> None:
        """Handle /download <remote_path> <local_path>."""
        parts = cmd.split()
        if len(parts) != 3:
            console.print(_markup("Usage: /download <remote_path> <local_path>", THEME.warning))
            return

        _, remote_path, local_path = parts
        try:
            sandbox = await self.session.get_sandbox()
            local = Path(local_path).expanduser().resolve()
            local.parent.mkdir(parents=True, exist_ok=True)
            await sandbox.fs.download_file(remote_path, str(local))
            console.print(_markup(f"Downloaded {remote_path} → {local}", THEME.success))
        except RuntimeError as exc:
            console.print(_markup(str(exc), THEME.error))
        except Exception as exc:
            console.print(_markup(f"Download failed: {exc}", THEME.error))

    def _handle_file_write_toggle(self, cmd: str) -> None:
        """Toggle the create-only write tool in readonly mode."""
        if self.mode_name != "readonly":
            console.print(
                _markup(
                    "File write toggling is only available in readonly mode.",
                    THEME.warning,
                )
            )
            return

        parts = cmd.split()
        has_write = "write" in self.session.registry

        if len(parts) == 1:
            if has_write:
                console.print(
                    _markup(
                        "File write is ON (create-only, approval required).",
                        THEME.success,
                    )
                )
                return
            console.print(
                _markup(
                    "Enable file write for exports? \\[y/N]:",
                    THEME.warning,
                ),
                end=" ",
            )
            try:
                response = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                response = "n"
            target = "on" if response in ("y", "yes") else "status"
        else:
            target = parts[1].lower()

        if target in ("status",):
            status = "ON" if has_write else "OFF"
            color = THEME.success if has_write else THEME.muted
            console.print(_markup(f"File write is {status}.", color))
            return

        if target in ("on", "enable"):
            if has_write:
                console.print(_markup("File write is already ON.", THEME.muted))
                return
            from ..tools.handlers.write import WriteHandler

            self.session.registry.register(WriteHandler(create_only=True, requires_approval=True))
            console.print(
                _markup(
                    "File write enabled for this session (create-only, approval required).",
                    THEME.success,
                )
            )
            return

        if target in ("off", "disable"):
            if not has_write:
                console.print(_markup("File write is already OFF.", THEME.muted))
                return
            self.session.registry.unregister("write")
            console.print(_markup("File write disabled for this session.", THEME.warning))
            return

        console.print(
            _markup(
                "Usage: /write [on|off|status]",
                THEME.warning,
            )
        )

    @staticmethod
    def _resolve_resume_id(raw: str, sessions: list[Any]) -> str | None:
        """Resolve user input (number, 'latest', id, or prefix) to a session ID."""
        target = raw.strip()
        if not target:
            return None
        if target.isdigit():
            idx = int(target)
            if 1 <= idx <= len(sessions):
                return sessions[idx - 1].id
            return None
        if target.lower() == "latest":
            return sessions[0].id if sessions else None
        for s in sessions:
            if s.id == target:
                return s.id
        matches = [s.id for s in sessions if s.id.startswith(target)]
        if len(matches) == 1:
            return matches[0]
        return None

    def _handle_resume(self, cmd: str) -> None:
        """Handle /resume: list or select a saved session to restore."""
        sessions = self.session_store.list(limit=20)
        if not sessions:
            console.print(_markup("No saved sessions to resume.", THEME.error))
            return

        parts = cmd.split(maxsplit=1)
        selected_id: str | None = None

        if len(parts) > 1:
            selected_id = self._resolve_resume_id(parts[1], sessions)
            if not selected_id:
                console.print(
                    _markup(
                        f"Could not resolve session '{parts[1]}'. Use /resume to list and select.",
                        THEME.warning,
                    )
                )
                return
        else:
            console.print(_markup("Recent sessions:", THEME.secondary))
            for idx, info in enumerate(sessions, start=1):
                try:
                    started_dt = datetime.fromisoformat(info.created_at)
                    time_str = started_dt.strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    time_str = info.id
                console.print(
                    f"{_markup(f'{idx:>2}.', THEME.secondary)} "
                    f"{_markup(info.id, THEME.accent)}  {time_str}  "
                    f"{_markup(info.model, THEME.muted)}"
                )
                if info.display_preview:
                    console.print(_markup(f"  {info.display_preview}", THEME.muted))
            console.print()
            console.print(
                _markup(
                    "Enter number, session ID, or prefix (blank to cancel):",
                    THEME.warning,
                ),
                end=" ",
            )
            try:
                raw = input().strip()
            except (EOFError, KeyboardInterrupt):
                raw = ""
            if not raw:
                console.print(_markup("Resume cancelled.", THEME.muted))
                return
            selected_id = self._resolve_resume_id(raw, sessions)
            if not selected_id:
                console.print(
                    _markup(
                        f"Could not resolve session '{raw}'.",
                        THEME.error,
                    )
                )
                return

        if selected_id == self.session.id:
            console.print(_markup("Already in that session.", THEME.muted))
            return
        selected = next((s for s in sessions if s.id == selected_id), None)
        if selected and selected.live and selected.pid != os.getpid():
            console.print(
                _markup(
                    f"Session {selected_id} is active in another process (pid {selected.pid}).",
                    THEME.error,
                )
            )
            return

        try:
            resumed = self.session_store.resume(selected_id)
        except FileNotFoundError:
            console.print(_markup(f"Session not found: {selected_id}", THEME.error))
            return

        # Continue as the resumed session: its history, ID and directory. The
        # agent (tools, profile, model) stays the one this process started with.
        previous_dir = self.session._session_dir
        self.session._state = resumed.state
        self.session._client = resumed.agent.create_client()
        self.session._id = resumed.id
        self.session._session_dir = resumed._session_dir
        if previous_dir is not None and previous_dir != resumed._session_dir:
            if (previous_dir / "trace.jsonl").exists():
                update_meta(previous_dir, status="completed")
            else:
                shutil.rmtree(previous_dir, ignore_errors=True)  # never used
        self.token_status.total_input_tokens = resumed.state.usage.get("input_tokens", 0)
        self.token_status.total_output_tokens = resumed.state.usage.get("output_tokens", 0)
        self.token_status.total_reasoning_tokens = resumed.state.usage.get("reasoning_tokens", 0)
        self.token_status.context_size = 0

        console.print(_markup(f"Resumed session: {selected_id}", THEME.success))
        console.print(
            _markup(
                f"Messages: {len(resumed.state.messages)}  "
                f"tokens in/out: {resumed.state.usage.get('input_tokens', 0)}/{resumed.state.usage.get('output_tokens', 0)}",
                THEME.muted,
            )
        )


async def run_interactive(
    session: Session,
    approval_handler: ApprovalHandler,
    mode_name: str,
    working_dir: str,
    session_store: SessionStore,
    *,
    upload_mappings: list[tuple[str, str]] | None = None,
    setup_commands: list[str] | None = None,
) -> None:
    """Run an interactive REPL session."""
    interactive = InteractiveSession(
        session=session,
        approval_handler=approval_handler,
        mode_name=mode_name,
        working_dir=working_dir,
        session_store=session_store,
        upload_mappings=upload_mappings,
        setup_commands=setup_commands,
    )
    await interactive.run()
