"""Interactive REPL session."""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import signal
from pathlib import Path
from typing import Any

from openai import APIError
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.markup import escape

from ..core.events import AgentEvent
from ..core.models import limits_for
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
    access_summary,
)
from .model_picker import pick_model
from .single import upload_to_sandbox
from .state import CONFIG_DIR, HISTORY_FILE, RENDER_MARKDOWN, console
from .theme import PTK_STYLE, THEME, set_theme
from .ui import (
    TurnRenderer,
    render_access_card,
    render_saved_sessions_table,
    render_session_status_card,
    render_themes_card,
    render_user_prompt,
    render_welcome_banner,
)


class InteractiveSession:
    """Encapsulates the mutable state and logic for an interactive REPL session."""

    def __init__(
        self,
        session: Session,
        approval_handler: ApprovalHandler,
        working_dir: str,
        session_store: SessionStore,
        upload_mappings: list[tuple[str, str]] | None = None,
        setup_commands: list[str] | None = None,
    ) -> None:
        self.session = session
        self.approval_handler = approval_handler
        self.approval_handler.working_dir = working_dir
        self.working_dir = working_dir
        self.session_store = session_store
        self.upload_mappings = upload_mappings or []
        self.setup_commands = setup_commands or []
        self.session_status = "completed"
        config = session.agent.config
        access_level = config.access.level.value if config.backend == "local" else "remote"
        self.token_status = TokenStatus(
            input_tokens=session.state.usage.get("input_tokens", 0),
            output_tokens=session.state.usage.get("output_tokens", 0),
            model=config.model or "",
            access_level=access_level,
            context_window=getattr(session, "context_window", None),
            cost_usd=float(session.state.usage.get("cost_usd", 0.0) or 0.0),
        )
        self.interactive_tty = _is_interactive_terminal()
        # Told to the model with the next message, e.g. after /access changes.
        self._pending_note: str | None = None

    def _prompt_tokens(self) -> list[tuple[str, str]]:
        """Minimal prompt reflecting current sandbox access posture."""
        level = self.token_status.access_level
        arrow_cls = {
            "read-only": "class:prompt.readonly",
            "full": "class:prompt.full",
        }.get(level, "class:prompt.arrow")
        return [(arrow_cls, "› ")]

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
            prompt_continuation=lambda width, line_number, is_soft_wrap: [
                ("class:continuation", "  · ")
            ],
            key_bindings=key_bindings,
            bottom_toolbar=self.token_status.render_toolbar,
            style=PTK_STYLE,
            erase_when_done=True,
            complete_while_typing=False,
            complete_in_thread=True,
        )

        # Welcome header card
        console.print(render_welcome_banner(self.session, self.working_dir, _get_version()))

        try:
            # Upload files to sandbox before starting interactive loop
            if self.upload_mappings:
                await upload_to_sandbox(self.session, self.upload_mappings)

            # Run setup commands after upload, before interactive loop
            if self.setup_commands:
                from .single import run_setup_commands

                await run_setup_commands(self.session, self.setup_commands, self.working_dir)

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
                    console.print(render_user_prompt(user_input, label="directive"))
                    console.print()
                    await self._execute_turn(user_input)
                    if self.session_status == "cancelled":
                        break
                    continue

                if not user_input:
                    continue

                if user_input.lower() in ("exit", "quit", "/exit", "/quit"):
                    break

                if user_input.startswith("/"):
                    if user_input.startswith("/download"):
                        await self._handle_download(user_input)
                        continue
                    if user_input.startswith("/access"):
                        self._handle_access(user_input)
                        continue
                    if user_input.startswith("/resume"):
                        self._handle_resume(user_input)
                        continue
                    if user_input == "/theme" or user_input.startswith("/theme "):
                        self._handle_theme(user_input)
                        continue
                    if user_input.split(maxsplit=1)[0] == "/model":
                        await self._handle_model(user_input)
                        continue
                    if user_input == "/status":
                        console.print(
                            render_session_status_card(
                                self.session, self.token_status, self.working_dir
                            )
                        )
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

                console.print(render_user_prompt(user_input))
                console.print()
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
                sid = escape(str(self.session.id))
                console.print(
                    f"\n[{THEME.muted}]Session saved ·[/{THEME.muted}] "
                    f"[{THEME.primary}]{sid}[/{THEME.primary}] "
                    f"[{THEME.muted}](rho-agent -r {sid})[/{THEME.muted}]"
                )
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

        prompt_task = asyncio.ensure_future(prompt_session.prompt_async(self._prompt_tokens))
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

        renderer = TurnRenderer(
            console=console,
            interactive_tty=self.interactive_tty,
            render_markdown=RENDER_MARKDOWN,
            working_dir=self.working_dir,
            model=self.session.agent.config.model or "",
            context_window=getattr(self.session, "context_window", None),
            token_status=self.token_status,
            show_turn_footer=False,
        )

        def on_cancel() -> None:
            renderer.pause_for_prompt()
            console.print(f"\n{_markup('Cancelling…', THEME.warning)}")
            self.session.cancel()

        if platform.system() != "Windows":
            loop.add_signal_handler(signal.SIGINT, on_cancel)

        prev_before = self.approval_handler.before_prompt
        prev_after = self.approval_handler.after_prompt
        self.approval_handler.before_prompt = renderer.pause_for_prompt
        self.approval_handler.after_prompt = renderer.resume_after_prompt
        try:
            renderer.start()

            async def on_event(event: AgentEvent) -> None:
                if event.type == "cancelled":
                    renderer.stop()
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
                renderer.on_event(event)

            if self._pending_note:
                user_input = f"{self._pending_note}\n\n{user_input}"
                self._pending_note = None
            await self.session.run(user_input, on_event=on_event)
        finally:
            renderer.stop()
            self.approval_handler.before_prompt = prev_before
            self.approval_handler.after_prompt = prev_after
            if platform.system() != "Windows":
                loop.remove_signal_handler(signal.SIGINT)

    async def _handle_model(self, cmd: str) -> None:
        parts = cmd.split(maxsplit=1)
        current = self.session.agent.config.model
        context_window = None
        if len(parts) > 1:
            model = parts[1].strip()
        else:
            try:
                with console.status("Loading model catalog…"):
                    models = await self.session.list_models()
                if not models:
                    raise ValueError("The provider returned no models.")
                selection = await pick_model(
                    models, current=current, openrouter=self.session.is_openrouter
                )
            except (KeyboardInterrupt, EOFError):
                return
            except (APIError, ValueError) as exc:
                status = getattr(exc, "status_code", None)
                if status == 401:
                    message = (
                        "The provider rejected authentication for /models (HTTP 401). "
                        "Check that RHO_AGENT_API_KEY matches the configured API endpoint, "
                        "then restart rho."
                    )
                else:
                    detail = f" (HTTP {status})" if status is not None else ""
                    message = f"Could not load models{detail}. Use /model <id> to choose by ID."
                console.print(_markup(message, THEME.warning))
                return
            if selection is None:
                return
            model, context_window = selection.id, selection.context_window

        try:
            self.session.switch_model(model, context_window=context_window)
        except (OSError, ValueError) as exc:
            console.print(_markup(f"Could not save the selected model. {exc}", THEME.error))
            return
        self.token_status.model = model
        self.token_status.context_window = (
            self.session.context_window or limits_for(model).context_window
        )
        if model != current:
            self.token_status.context_size = self.session.state.estimate_tokens(
                self.session.agent.system_prompt
            )
        label = "Model switched to" if model != current else "Current model:"
        console.print(_markup(f"{label} {model}", THEME.secondary))

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
            console.print(_markup(f"✓ Downloaded {remote_path} → {local}", THEME.success))
        except RuntimeError as exc:
            console.print(_markup(str(exc), THEME.error))
        except Exception as exc:
            console.print(_markup(f"Download failed: {exc}", THEME.error))

    def _handle_theme(self, cmd: str) -> None:
        """``/theme`` lists palettes; ``/theme <name>`` switches live and saves preference."""
        parts = cmd.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            console.print(render_themes_card(command_prefix="/theme"))
            return
        choice = parts[1].strip()
        try:
            chosen = set_theme(choice, persist=True)
        except ValueError as exc:
            console.print(_markup(str(exc), THEME.error))
            return
        console.print(
            _markup(f"✓ Theme set to {chosen.name} ({chosen.description})", THEME.success)
        )

    def _handle_access(self, cmd: str) -> None:
        """``/access`` shows access; ``/access <level>`` changes it;
        ``/access revoke <path>`` removes a grant."""
        parts = cmd.split(maxsplit=2)
        session = self.session
        agent = session.agent
        if len(parts) == 1:
            self._show_access()
            return
        if parts[1] == "revoke":
            if len(parts) < 3:
                console.print(_markup("Usage: /access revoke <path>", THEME.warning))
                return
            target = str(Path(parts[2]).expanduser().resolve())
            if session.revoke_access(target) or session.revoke_access(parts[2]):
                console.print(_markup(f"✓ Revoked access to {parts[2]}.", THEME.success))
                self._pending_note = f"[The user revoked your access to {parts[2]}.]"
            else:
                console.print(_markup(f"No grant for {parts[2]}.", THEME.warning))
            return
        if agent.config.backend != "local":
            console.print(_markup("Access is fixed for remote backends.", THEME.warning))
            return

        from ..sandbox import AccessLevel, SandboxUnavailableError
        from .main_cmd import _default_prompt_for

        try:
            level = AccessLevel(parts[1])
        except ValueError:
            console.print(
                _markup(
                    "Usage: /access [read-only|workspace|full] or /access revoke <path>",
                    THEME.warning,
                )
            )
            return
        config = agent.config
        previous = config.access
        if level == previous.level:
            console.print(_markup(f"Access is already {level.value}.", THEME.muted))
            return
        default_prompt = _default_prompt_for(config)
        config.access = previous.with_level(level)
        try:
            agent._registry = agent._build_registry()
        except SandboxUnavailableError as exc:
            config.access = previous
            console.print(_markup(str(exc), THEME.error))
            return
        session._registry = agent.registry
        self.token_status.access_level = level.value
        if config.system_prompt and config.system_prompt == default_prompt:
            config.system_prompt = _default_prompt_for(config) or config.system_prompt
            agent._system_prompt = None
        self._pending_note = (
            f"[The user changed your access to {level.value}. Tools and limits have "
            "changed accordingly.]"
        )
        console.print(_markup(f"✓ {access_summary(agent)}", THEME.success))

    def _show_access(self) -> None:
        console.print(render_access_card(self.session))

    @staticmethod
    def _resolve_resume_id(raw: str, sessions: list[Any]) -> str | None:
        """Resolve user input (number, 'latest', id, or prefix) to a session ID."""
        target = raw.strip()
        if not target:
            return None
        # A full ID wins over a list number: IDs are hex, and about one in
        # forty has no letters in it.
        for s in sessions:
            if s.id == target:
                return s.id
        if target.isdigit():
            idx = int(target)
            if 1 <= idx <= len(sessions):
                return sessions[idx - 1].id
        if target.lower() == "latest":
            return sessions[0].id if sessions else None
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
            console.print(render_saved_sessions_table(sessions, numbered=True))
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
        # agent keeps its tools and access. Use the resumed connection settings
        # for both generation and the model picker.
        previous_dir = self.session._session_dir
        self.session._state = resumed.state
        self.session._client = resumed._client
        config = self.session.agent.config
        saved = resumed.agent.config
        config.model = saved.model
        config.base_url = saved.base_url
        config.service_tier = saved.service_tier
        config.reasoning_effort = saved.reasoning_effort
        config.response_format = saved.response_format
        config.context_window = saved.context_window
        self.session.context_window = resumed.context_window
        self.token_status.model = saved.model
        self.token_status.context_window = (
            resumed.context_window or limits_for(saved.model).context_window
        )
        self.session._id = resumed.id
        self.session._session_dir = resumed._session_dir
        self.session.access_grants = list(resumed.access_grants)
        if previous_dir is not None and previous_dir != resumed._session_dir:
            if (previous_dir / "trace.jsonl").exists():
                update_meta(previous_dir, status="completed")
            else:
                shutil.rmtree(previous_dir, ignore_errors=True)  # never used
        _sync_token_status_from_state(self.token_status, resumed.state)
        self.token_status.context_size = 0

        console.print(_markup(f"✓ Resumed session #{selected_id}", THEME.success))
        console.print(
            _markup(
                f"  Messages: {len(resumed.state.messages)}  ·  "
                f"tokens in/out: {resumed.state.usage.get('input_tokens', 0)}/"
                f"{resumed.state.usage.get('output_tokens', 0)}",
                THEME.muted,
            )
        )


async def run_interactive(
    session: Session,
    approval_handler: ApprovalHandler,
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
        working_dir=working_dir,
        session_store=session_store,
        upload_mappings=upload_mappings,
        setup_commands=setup_commands,
    )
    await interactive.run()
