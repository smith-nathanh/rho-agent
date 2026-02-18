"""Interactive REPL session."""

import asyncio
import platform
import signal
from datetime import datetime
from time import monotonic
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.panel import Panel

from ..core.agent import AgentEvent
from ..core.conversations import ConversationStore
from ..runtime.types import LocalRuntime
from ..signals import SignalManager
from ..ui.theme import THEME
from .completion import create_completer
from .errors import InvalidProfileError
from .events import ApprovalHandler, handle_command, handle_event, switch_runtime_profile
from .formatting import (
    TokenStatus,
    _get_version,
    _is_interactive_terminal,
    _markup,
    _sync_token_status_from_session,
    _wait_while_paused,
)
from .state import CONFIG_DIR, HISTORY_FILE, RENDER_MARKDOWN, console


class InteractiveSession:
    """Encapsulates the mutable state and logic for an interactive REPL session."""

    def __init__(
        self,
        runtime: LocalRuntime,
        approval_handler: ApprovalHandler,
        mode_name: str,
        working_dir: str,
        conversation_store: ConversationStore,
        session_started: datetime,
        conversation_id: str | None = None,
        signal_manager: SignalManager | None = None,
        session_id: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.approval_handler = approval_handler
        self.mode_name = mode_name
        self.working_dir = working_dir
        self.conversation_store = conversation_store
        self.session_started = session_started
        self.conversation_id = conversation_id
        self.signal_manager = signal_manager
        self.session_id = session_id
        self.session_status = "completed"
        self.token_status = TokenStatus(
            input_tokens=runtime.session.total_input_tokens,
            output_tokens=runtime.session.total_output_tokens,
        )
        self.interactive_tty = _is_interactive_terminal()

    async def run(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        await self.runtime.start()

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
        obs_status = (
            _markup("telemetry enabled", THEME.success)
            if self.runtime.observability
            else _markup("telemetry off", THEME.muted)
        )
        version = _get_version()
        console.print(
            Panel(
                f"[bold]{_markup('ρ rho-agent', THEME.primary)}[/bold] v{version}\n"
                f"Mode: {_markup(self.mode_name, THEME.accent)}\n"
                f"Model: {_markup(self.runtime.model, THEME.accent)} {obs_status}\n"
                "Enter to send, Esc+Enter for newline, Ctrl+C to cancel.\n"
                "Type [bold]/help[/bold] for commands, [bold]exit[/bold] to quit.",
                border_style=THEME.border,
            )
        )

        try:
            while True:
                if self.signal_manager and self.session_id:
                    if not await _wait_while_paused(self.signal_manager, self.session_id):
                        self.session_status = "cancelled"
                        if self.runtime.observability:
                            self.runtime.observability.context.metadata["cancel_source"] = "kill_command"
                        console.print(_markup("Killed by rho-agent kill", THEME.warning))
                        break

                    directives = self.signal_manager.consume_directives(self.session_id)
                    for directive in directives:
                        console.print(_markup(f"Directive received: {directive}", THEME.secondary))
                        await self._execute_turn(directive)
                        if self.session_status == "cancelled":
                            break
                    if self.session_status == "cancelled":
                        break

                    if self.signal_manager.has_export_request(self.session_id):
                        from ..context_export import write_context_file

                        write_context_file(
                            self.signal_manager.context_path(self.session_id),
                            self.runtime.session.get_messages(),
                        )
                        self.signal_manager.clear_export_request(self.session_id)

                try:
                    console.print()
                    user_input = await prompt_session.prompt_async(
                        HTML(f"<style fg='{THEME.prompt}'><b>&gt;</b></style> ")
                    )
                    user_input = user_input.strip()
                except (EOFError, KeyboardInterrupt):
                    break

                if not user_input:
                    continue

                if user_input.lower() in ("exit", "quit"):
                    break

                if user_input.startswith("/"):
                    if user_input.startswith("/write"):
                        self._handle_file_write_toggle(user_input)
                        continue
                    if user_input.startswith("/mode"):
                        self._handle_mode_switch(user_input)
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
                        result = await self.runtime.agent.compact(
                            custom_instructions=instructions, trigger="manual"
                        )
                        handle_event(
                            AgentEvent(
                                type="compact_end",
                                content=f"Compacted: {result.tokens_before} → {result.tokens_after} tokens",
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
            await self.runtime.close(self.session_status)

            if self.runtime.session.history:
                saved_path = self.conversation_store.save(
                    model=self.runtime.model,
                    system_prompt=self.runtime.session.system_prompt,
                    history=self.runtime.session.history,
                    input_tokens=self.runtime.session.total_input_tokens,
                    output_tokens=self.runtime.session.total_output_tokens,
                    started=self.session_started,
                    conversation_id=self.conversation_id,
                )
                console.print(f"\n[dim]Goodbye! Conversation saved to {saved_path}[/dim]")
            else:
                console.print(_markup("\nGoodbye!", THEME.muted))

    async def _execute_turn(self, user_input: str) -> None:
        loop = asyncio.get_event_loop()
        response_chunks: list[str] = []

        def on_cancel():
            console.print(f"\n{_markup('Cancelling...', THEME.warning)}")
            self.runtime.agent.request_cancel()

        if platform.system() != "Windows":
            loop.add_signal_handler(signal.SIGINT, on_cancel)

        try:
            events = self.runtime.agent.run_turn(user_input)
            if self.runtime.observability:
                events = self.runtime.observability.wrap_turn(events, user_input)

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

            async for event in events:
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
                    _sync_token_status_from_session(self.token_status, self.runtime.session)
                    if self.signal_manager and self.session_id and self.signal_manager.is_cancelled(self.session_id):
                        self.session_status = "cancelled"
                        if self.runtime.observability:
                            self.runtime.observability.context.metadata["cancel_source"] = "kill_command"
                        console.print(_markup("Killed by rho-agent kill", THEME.warning))
                    else:
                        console.print(_markup("Turn cancelled", THEME.muted))
                    break
                if event.type == "error":
                    _sync_token_status_from_session(self.token_status, self.runtime.session)
                handle_event(
                    event,
                    show_turn_usage=False,
                    token_status=self.token_status,
                    render_markdown=self.interactive_tty and RENDER_MARKDOWN,
                    pending_text_chunks=pending_text_chunks,
                )
            if status_ctx:
                status_ctx.__exit__(None, None, None)
            if self.signal_manager and self.session_id and response_chunks:
                self.signal_manager.record_response(self.session_id, "".join(response_chunks))
        finally:
            if platform.system() != "Windows":
                loop.remove_signal_handler(signal.SIGINT)

    def _handle_file_write_toggle(self, cmd: str) -> None:
        if self.mode_name != "readonly":
            console.print(
                _markup(
                    "File write toggling is only available in readonly mode.",
                    THEME.warning,
                )
            )
            return

        parts = cmd.split()
        has_write = "write" in self.runtime.registry

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

            self.runtime.registry.register(WriteHandler(create_only=True, requires_approval=True))
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
            self.runtime.registry.unregister("write")
            console.print(_markup("File write disabled for this session.", THEME.warning))
            return

        console.print(
            _markup(
                "Usage: /write [on|off|status]",
                THEME.warning,
            )
        )

    @staticmethod
    def _resolve_resume_id(raw: str, conversations: list[Any]) -> str | None:
        target = raw.strip()
        if not target:
            return None
        if target.isdigit():
            idx = int(target)
            if 1 <= idx <= len(conversations):
                return conversations[idx - 1].id
            return None
        if target.lower() == "latest":
            return conversations[0].id if conversations else None
        for conv in conversations:
            if conv.id == target:
                return conv.id
        matches = [conv.id for conv in conversations if conv.id.startswith(target)]
        if len(matches) == 1:
            return matches[0]
        return None

    def _handle_resume(self, cmd: str) -> None:
        conversations = self.conversation_store.list_conversations(limit=20)
        if not conversations:
            console.print(_markup("No saved conversations to resume.", THEME.error))
            return

        parts = cmd.split(maxsplit=1)
        selected_id: str | None = None

        if len(parts) > 1:
            selected_id = self._resolve_resume_id(parts[1], conversations)
            if not selected_id:
                console.print(
                    _markup(
                        f"Could not resolve conversation '{parts[1]}'. "
                        "Use /resume to list and select.",
                        THEME.warning,
                    )
                )
                return
        else:
            console.print(_markup("Recent conversations:", THEME.secondary))
            for idx, conv in enumerate(conversations, start=1):
                try:
                    started_dt = datetime.fromisoformat(conv.started)
                    time_str = started_dt.strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    time_str = conv.id
                console.print(
                    f"{_markup(f'{idx:>2}.', THEME.secondary)} "
                    f"{_markup(conv.id, THEME.accent)}  {time_str}  "
                    f"{_markup(conv.model, THEME.muted)}"
                )
                if conv.display_preview:
                    console.print(_markup(f"  {conv.display_preview}", THEME.muted))
            console.print()
            console.print(
                _markup(
                    "Enter number, conversation ID, or prefix (blank to cancel):",
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
            selected_id = self._resolve_resume_id(raw, conversations)
            if not selected_id:
                console.print(
                    _markup(
                        f"Could not resolve conversation '{raw}'.",
                        THEME.error,
                    )
                )
                return

        resumed = self.conversation_store.load(selected_id)
        if not resumed:
            console.print(_markup(f"Conversation not found: {selected_id}", THEME.error))
            return

        self.runtime.session.system_prompt = resumed.system_prompt
        self.runtime.session.history = resumed.history.copy()
        self.runtime.session.total_input_tokens = resumed.input_tokens
        self.runtime.session.total_output_tokens = resumed.output_tokens
        self.token_status.context_size = 0
        self.token_status.total_input_tokens = resumed.input_tokens
        self.token_status.total_output_tokens = resumed.output_tokens
        self.conversation_id = resumed.id
        try:
            self.session_started = datetime.fromisoformat(resumed.started)
        except ValueError:
            self.session_started = datetime.now()

        console.print(_markup(f"Resumed conversation: {resumed.id}", THEME.success))
        console.print(
            _markup(
                f"Messages: {len(resumed.history)}  "
                f"tokens in/out: {resumed.input_tokens}/{resumed.output_tokens}",
                THEME.muted,
            )
        )

    def _handle_mode_switch(self, cmd: str) -> None:
        parts = cmd.split(maxsplit=1)
        if len(parts) == 1 or parts[1].strip().lower() in ("status", "current"):
            console.print(_markup(f"Current mode: {self.mode_name}", THEME.accent))
            console.print(
                _markup(
                    "Usage: /mode <readonly|developer|eval|profile-path>",
                    THEME.muted,
                )
            )
            return

        target = parts[1].strip()
        try:
            capability_profile = switch_runtime_profile(
                self.runtime,
                target,
                working_dir=self.working_dir,
            )
        except InvalidProfileError as e:
            console.print(_markup(str(e), THEME.error))
            return

        self.mode_name = capability_profile.name
        console.print(_markup(f"Switched mode to {self.mode_name}", THEME.success))
        console.print(
            _markup(
                (
                    "shell="
                    f"{capability_profile.shell.value}, "
                    "file_write="
                    f"{capability_profile.file_write.value}, "
                    "database="
                    f"{capability_profile.database.value}"
                ),
                THEME.muted,
            )
        )


async def run_interactive(
    runtime: LocalRuntime,
    approval_handler: ApprovalHandler,
    mode_name: str,
    working_dir: str,
    conversation_store: ConversationStore,
    session_started: datetime,
    conversation_id: str | None = None,
    signal_manager: SignalManager | None = None,
    session_id: str | None = None,
) -> None:
    """Run an interactive REPL session."""
    session = InteractiveSession(
        runtime=runtime,
        approval_handler=approval_handler,
        mode_name=mode_name,
        working_dir=working_dir,
        conversation_store=conversation_store,
        session_started=session_started,
        conversation_id=conversation_id,
        signal_manager=signal_manager,
        session_id=session_id,
    )
    await session.run()
