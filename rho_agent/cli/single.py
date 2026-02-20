"""Single-shot execution: run_single, run_single_with_output."""

from __future__ import annotations

import asyncio
import platform
import signal
from pathlib import Path
from time import monotonic

from ..runtime.types import LocalRuntime
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
    runtime: LocalRuntime,
    prompt: str,
    signal_manager: SignalManager | None = None,
    session_id: str | None = None,
) -> None:
    """Run a single prompt and exit."""
    # Start observability session if enabled
    await runtime.start()

    loop = asyncio.get_event_loop()
    interactive_tty = _is_interactive_terminal()

    def on_cancel():
        console.print(f"\n{_markup('Cancelling...', THEME.warning)}")
        runtime.agent.request_cancel()

    if platform.system() != "Windows":
        loop.add_signal_handler(signal.SIGINT, on_cancel)

    session_status = "completed"
    try:
        if signal_manager and session_id:
            if not await _wait_while_paused(signal_manager, session_id):
                session_status = "cancelled"
                if runtime.observability:
                    runtime.observability.context.metadata["cancel_source"] = "kill_command"
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

        # Wrap event stream with observability if enabled
        events = runtime.agent.run_turn(prompt)
        if runtime.observability:
            events = runtime.observability.wrap_turn(events, prompt)

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

        async for event in events:
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
                break
            if event.type == "cancelled":
                if signal_manager and session_id and signal_manager.is_cancelled(session_id):
                    session_status = "cancelled"
                    if runtime.observability:
                        runtime.observability.context.metadata["cancel_source"] = "kill_command"
                    console.print(_markup("Killed by rho-agent kill", THEME.warning))
                else:
                    console.print(_markup("Cancelled", THEME.muted))
                break
            handle_event(
                event,
                render_markdown=interactive_tty and RENDER_MARKDOWN,
                pending_text_chunks=pending_text_chunks,
            )
        if status_ctx:
            status_ctx.__exit__(None, None, None)
    except Exception:
        session_status = "error"
        raise
    finally:
        if platform.system() != "Windows":
            loop.remove_signal_handler(signal.SIGINT)
        await runtime.close(session_status)


async def run_single_with_output(
    runtime: LocalRuntime,
    prompt: str,
    output_path: str,
    signal_manager: SignalManager | None = None,
    session_id: str | None = None,
) -> bool:
    """Run a single prompt and write final response to file.

    Returns True if successful, False if output file already exists.
    """
    output_file = Path(output_path).expanduser().resolve()

    # Check if output file already exists before running
    if output_file.exists():
        console.print(_markup(f"Output file already exists: {output_file}", THEME.error))
        console.print(
            _markup(
                "Use a different path or delete the existing file first.",
                THEME.muted,
            )
        )
        return False

    # Start observability session if enabled
    await runtime.start()

    collected_text: list[str] = []
    cancelled = False
    had_error = False

    loop = asyncio.get_event_loop()
    interactive_tty = _is_interactive_terminal()

    def on_cancel():
        console.print(f"\n{_markup('Cancelling...', THEME.warning)}")
        runtime.agent.request_cancel()

    if platform.system() != "Windows":
        loop.add_signal_handler(signal.SIGINT, on_cancel)

    session_status = "completed"
    try:
        if signal_manager and session_id:
            if not await _wait_while_paused(signal_manager, session_id):
                session_status = "cancelled"
                if runtime.observability:
                    runtime.observability.context.metadata["cancel_source"] = "kill_command"
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

        # Wrap event stream with observability if enabled
        events = runtime.agent.run_turn(prompt)
        if runtime.observability:
            events = runtime.observability.wrap_turn(events, prompt)

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

        async for event in events:
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
                had_error = True
                handle_event(
                    event,
                    render_markdown=interactive_tty and RENDER_MARKDOWN,
                    pending_text_chunks=pending_text_chunks,
                )
                break
            if event.type == "cancelled":
                if signal_manager and session_id and signal_manager.is_cancelled(session_id):
                    session_status = "cancelled"
                    if runtime.observability:
                        runtime.observability.context.metadata["cancel_source"] = "kill_command"
                    console.print(_markup("Killed by rho-agent kill", THEME.warning))
                else:
                    console.print(_markup("Cancelled", THEME.muted))
                cancelled = True
                break
            handle_event(
                event,
                render_markdown=interactive_tty and RENDER_MARKDOWN,
                pending_text_chunks=pending_text_chunks,
            )
            # Collect text for output file
            if event.type == "text" and event.content:
                collected_text.append(event.content)
        if status_ctx:
            status_ctx.__exit__(None, None, None)
    except Exception:
        session_status = "error"
        raise
    finally:
        if platform.system() != "Windows":
            loop.remove_signal_handler(signal.SIGINT)
        await runtime.close(session_status)

    if cancelled:
        return False
    if had_error:
        return False

    # Write collected text to output file
    try:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("".join(collected_text), encoding="utf-8")
        console.print(f"\n{_markup(f'Output written to: {output_file}', THEME.success)}")
        return True
    except Exception as exc:
        console.print(f"\n{_markup(f'Failed to write output: {exc}', THEME.error)}")
        return False
