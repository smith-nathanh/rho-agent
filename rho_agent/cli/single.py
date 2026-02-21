"""Single-shot execution: run_single, run_single_with_output."""

from __future__ import annotations

import asyncio
import platform
import signal
from pathlib import Path
from time import monotonic

from ..core.events import AgentEvent
from ..core.session import Session
from ..signals import SignalManager
from .theme import THEME
from .events import handle_event
from .formatting import (
    _is_interactive_terminal,
    _markup,
    _wait_while_paused,
)
from .state import RENDER_MARKDOWN, console


async def run_single(
    session: Session,
    prompt: str,
    signal_manager: SignalManager | None = None,
    session_id: str | None = None,
) -> None:
    """Run a single prompt and exit."""
    loop = asyncio.get_event_loop()
    interactive_tty = _is_interactive_terminal()

    def on_cancel():
        console.print(f"\n{_markup('Cancelling...', THEME.warning)}")
        session.cancel()

    if platform.system() != "Windows":
        loop.add_signal_handler(signal.SIGINT, on_cancel)

    session_status = "completed"
    try:
        if signal_manager and session_id:
            if not await _wait_while_paused(signal_manager, session_id):
                console.print(_markup("Killed by rho-agent kill", THEME.warning))
                return
            directives = signal_manager.consume_directives(session_id)
            if directives:
                console.print(
                    _markup(
                        "Ignoring queued directives in single-prompt mode.",
                        THEME.muted,
                    )
                )

        status_ctx = None
        start = monotonic()
        if interactive_tty:
            status_ctx = console.status(
                "⠋ working (0s)",
                spinner="dots",
                spinner_style=THEME.accent,
            )
            status_ctx.__enter__()
        saw_model_output = False
        pending_text_chunks: list[str] = []

        async def on_event(event: AgentEvent) -> None:
            nonlocal saw_model_output, status_ctx, session_status, pending_text_chunks
            if status_ctx and not saw_model_output:
                elapsed = int(monotonic() - start)
                status_ctx.update(f"⠋ working ({elapsed}s • Ctrl+C: cancel)")
            if event.type in ("text", "tool_start", "error", "cancelled"):
                saw_model_output = True
                if status_ctx:
                    status_ctx.__exit__(None, None, None)
                    status_ctx = None
            if event.type == "error":
                session_status = "error"
                handle_event(
                    event,
                    render_markdown=interactive_tty and RENDER_MARKDOWN,
                    pending_text_chunks=pending_text_chunks,
                )
                return
            if event.type == "cancelled":
                if signal_manager and session_id and signal_manager.is_cancelled(session_id):
                    session_status = "cancelled"
                    console.print(_markup("Killed by rho-agent kill", THEME.warning))
                else:
                    console.print(_markup("Cancelled", THEME.muted))
                return
            handle_event(
                event,
                render_markdown=interactive_tty and RENDER_MARKDOWN,
                pending_text_chunks=pending_text_chunks,
            )

        result = await session.run(prompt, on_event=on_event)

        if status_ctx:
            status_ctx.__exit__(None, None, None)
    except Exception:
        session_status = "error"
        raise
    finally:
        if platform.system() != "Windows":
            loop.remove_signal_handler(signal.SIGINT)


async def run_single_with_output(
    session: Session,
    prompt: str,
    output_path: str,
    signal_manager: SignalManager | None = None,
    session_id: str | None = None,
) -> bool:
    """Run a single prompt and write final response to file.

    Returns True if successful, False if output file already exists.
    """
    output_file = Path(output_path).expanduser().resolve()

    if output_file.exists():
        console.print(_markup(f"Output file already exists: {output_file}", THEME.error))
        console.print(
            _markup(
                "Use a different path or delete the existing file first.",
                THEME.muted,
            )
        )
        return False

    loop = asyncio.get_event_loop()
    interactive_tty = _is_interactive_terminal()

    def on_cancel():
        console.print(f"\n{_markup('Cancelling...', THEME.warning)}")
        session.cancel()

    if platform.system() != "Windows":
        loop.add_signal_handler(signal.SIGINT, on_cancel)

    collected_text: list[str] = []
    cancelled = False
    had_error = False
    pending_text_chunks: list[str] = []

    try:
        if signal_manager and session_id:
            if not await _wait_while_paused(signal_manager, session_id):
                console.print(_markup("Killed by rho-agent kill", THEME.warning))
                return False
            directives = signal_manager.consume_directives(session_id)
            if directives:
                console.print(
                    _markup(
                        "Ignoring queued directives in single-prompt mode.",
                        THEME.muted,
                    )
                )

        status_ctx = None
        start = monotonic()
        if interactive_tty:
            status_ctx = console.status(
                "⠋ working (0s)",
                spinner="dots",
                spinner_style=THEME.accent,
            )
            status_ctx.__enter__()
        saw_model_output = False

        async def on_event(event: AgentEvent) -> None:
            nonlocal saw_model_output, status_ctx, cancelled, had_error
            if status_ctx and not saw_model_output:
                elapsed = int(monotonic() - start)
                status_ctx.update(f"⠋ working ({elapsed}s • Ctrl+C: cancel)")
            if event.type in ("text", "tool_start", "error", "cancelled"):
                saw_model_output = True
                if status_ctx:
                    status_ctx.__exit__(None, None, None)
                    status_ctx = None
            if event.type == "error":
                had_error = True
                handle_event(
                    event,
                    render_markdown=interactive_tty and RENDER_MARKDOWN,
                    pending_text_chunks=pending_text_chunks,
                )
                return
            if event.type == "cancelled":
                if signal_manager and session_id and signal_manager.is_cancelled(session_id):
                    console.print(_markup("Killed by rho-agent kill", THEME.warning))
                else:
                    console.print(_markup("Cancelled", THEME.muted))
                cancelled = True
                return
            handle_event(
                event,
                render_markdown=interactive_tty and RENDER_MARKDOWN,
                pending_text_chunks=pending_text_chunks,
            )
            if event.type == "text" and event.content:
                collected_text.append(event.content)

        result = await session.run(prompt, on_event=on_event)

        if status_ctx:
            status_ctx.__exit__(None, None, None)
    except Exception:
        raise
    finally:
        if platform.system() != "Windows":
            loop.remove_signal_handler(signal.SIGINT)

    if cancelled or had_error:
        return False

    try:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("".join(collected_text), encoding="utf-8")
        console.print(f"\n{_markup(f'Output written to: {output_file}', THEME.success)}")
        return True
    except Exception as exc:
        console.print(f"\n{_markup(f'Failed to write output: {exc}', THEME.error)}")
        return False
