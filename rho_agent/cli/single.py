"""Single-shot execution: run_single, run_single_with_output."""

from __future__ import annotations

import asyncio
import platform
import signal
from pathlib import Path

from ..core.events import AgentEvent
from ..core.session import Session
from .events import handle_event
from .formatting import (
    _is_interactive_terminal,
    _markup,
)
from .state import RENDER_MARKDOWN, console
from .theme import THEME
from .ui import TurnRenderer

_UPLOAD_BATCH_SIZE = 100

# Directories to skip when uploading (large, generated, or irrelevant)
_UPLOAD_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox"}


async def upload_to_sandbox(
    session: Session,
    mappings: list[tuple[str, str]],
) -> None:
    """Upload local files/directories to the Daytona sandbox.

    Each mapping is (local_src, remote_dest). Directories are walked recursively.
    Directories go up in bulk writes of _UPLOAD_BATCH_SIZE files.
    """
    from rho_agent.tools.handlers.daytona.ops import upload_paths

    if not mappings:
        return

    console.print(_markup("  ◌ Creating remote sandbox…", THEME.muted))
    sandbox = await session.get_sandbox()
    manager = session.agent.sandbox_manager

    for src_str, dest in mappings:
        # Relative destinations are under the sandbox working directory; the
        # v2 file API takes absolute paths only.
        dest = await manager.absolute_path(dest)
        src = Path(src_str).expanduser().resolve()
        if not src.exists():
            console.print(_markup(f"  ✗ Upload source not found: {src}", THEME.error))
            continue

        if src.is_dir():
            files = [
                f
                for f in src.rglob("*")
                if f.is_file() and not (_UPLOAD_SKIP_DIRS & set(f.relative_to(src).parts))
            ]
            total = len(files)
            console.print(
                _markup(f"  ↑ Uploading {src_str} → {dest} ({total} files)", THEME.accent)
            )
            done = 0
            for i in range(0, total, _UPLOAD_BATCH_SIZE):
                batch = files[i : i + _UPLOAD_BATCH_SIZE]
                await upload_paths(
                    sandbox,
                    [(fp, f"{dest.rstrip('/')}/{fp.relative_to(src)}") for fp in batch],
                )
                done += len(batch)
                console.print(_markup(f"    {done}/{total} files uploaded", THEME.muted))
        else:
            console.print(_markup(f"  ↑ Uploading {src_str} → {dest}", THEME.accent))
            await sandbox.fs.upload_file(str(src), dest)


async def run_setup_commands(
    session: Session,
    commands: list[str],
    working_dir: str,
) -> None:
    """Run setup commands from .rho-agent.toml on the sandbox.

    Raises RuntimeError on non-zero exit to fail fast before the agent starts.
    """
    from rho_agent.tools.handlers.daytona.ops import run

    if not commands:
        return

    sandbox = await session.get_sandbox()

    # Get env vars from the sandbox manager so setup commands have the right PATH
    manager = session.agent.sandbox_manager
    env = manager.env_vars if manager else None

    for cmd in commands:
        console.print(_markup(f"  ❯ setup  $ {cmd}", THEME.accent))
        resp = await run(sandbox, cmd, cwd=working_dir, env=env)
        if resp.output:
            console.print(_markup(resp.output.rstrip(), THEME.muted))
        if resp.exit_code != 0:
            raise RuntimeError(f"Setup command failed (exit {resp.exit_code}): {cmd}")


async def run_single(
    session: Session,
    prompt: str,
    *,
    upload_mappings: list[tuple[str, str]] | None = None,
    setup_commands: list[str] | None = None,
) -> None:
    """Run a single prompt and exit."""
    loop = asyncio.get_event_loop()
    interactive_tty = _is_interactive_terminal()
    working_dir = getattr(getattr(session, "agent", None), "config", None)
    wd_str = getattr(working_dir, "working_dir", None)
    model_str = getattr(working_dir, "model", "") or ""

    renderer: TurnRenderer | None = None
    if interactive_tty:
        renderer = TurnRenderer(
            console=console,
            interactive_tty=True,
            render_markdown=RENDER_MARKDOWN,
            working_dir=wd_str,
            model=model_str,
            context_window=getattr(session, "context_window", None),
            show_turn_footer=True,
        )

    def on_cancel() -> None:
        if renderer is not None:
            renderer.pause_for_prompt()
        console.print(f"\n{_markup('⊘ Cancelling…', THEME.warning)}")
        session.cancel()

    if platform.system() != "Windows":
        loop.add_signal_handler(signal.SIGINT, on_cancel)

    try:
        # Upload files to sandbox before running
        if upload_mappings:
            await upload_to_sandbox(session, upload_mappings)

        # Run setup commands after upload, before agent starts
        if setup_commands:
            await run_setup_commands(session, setup_commands, session.agent.config.working_dir)

        if renderer is not None:
            renderer.start()
        pending_text_chunks: list[str] = []

        async def on_event(event: AgentEvent) -> None:
            if renderer is not None:
                if event.type == "cancelled":
                    renderer.stop()
                    console.print(_markup("⊘ Cancelled", THEME.muted))
                    return
                renderer.on_event(event)
                return

            if event.type == "cancelled":
                console.print(_markup("Cancelled", THEME.muted))
                return
            handle_event(
                event,
                render_markdown=False,
                pending_text_chunks=pending_text_chunks,
                working_dir=wd_str,
                model=model_str,
            )

        await session.run(prompt, on_event=on_event)
    finally:
        if renderer is not None:
            renderer.stop()
        if platform.system() != "Windows":
            loop.remove_signal_handler(signal.SIGINT)
        await session.close()


async def run_single_with_output(
    session: Session,
    prompt: str,
    output_path: str,
    *,
    upload_mappings: list[tuple[str, str]] | None = None,
    setup_commands: list[str] | None = None,
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
    cfg = getattr(getattr(session, "agent", None), "config", None)
    wd_str = getattr(cfg, "working_dir", None)
    model_str = getattr(cfg, "model", "") or ""

    renderer: TurnRenderer | None = None
    if interactive_tty:
        renderer = TurnRenderer(
            console=console,
            interactive_tty=True,
            render_markdown=RENDER_MARKDOWN,
            working_dir=wd_str,
            model=model_str,
            context_window=getattr(session, "context_window", None),
            show_turn_footer=True,
        )

    def on_cancel() -> None:
        if renderer is not None:
            renderer.pause_for_prompt()
        console.print(f"\n{_markup('⊘ Cancelling…', THEME.warning)}")
        session.cancel()

    if platform.system() != "Windows":
        loop.add_signal_handler(signal.SIGINT, on_cancel)

    collected_text: list[str] = []
    cancelled = False
    had_error = False
    pending_text_chunks: list[str] = []

    try:
        # Upload files to sandbox before running
        if upload_mappings:
            await upload_to_sandbox(session, upload_mappings)

        # Run setup commands after upload, before agent starts
        if setup_commands:
            await run_setup_commands(session, setup_commands, session.agent.config.working_dir)

        if renderer is not None:
            renderer.start()

        async def on_event(event: AgentEvent) -> None:
            nonlocal cancelled, had_error
            if event.type == "text" and event.content:
                collected_text.append(event.content)
            if event.type == "error":
                had_error = True
            if event.type == "cancelled":
                cancelled = True
                if renderer is not None:
                    renderer.stop()
                console.print(_markup("Cancelled", THEME.muted))
                return

            if renderer is not None:
                renderer.on_event(event)
                return

            handle_event(
                event,
                render_markdown=False,
                pending_text_chunks=pending_text_chunks,
                working_dir=wd_str,
                model=model_str,
            )

        await session.run(prompt, on_event=on_event)
    finally:
        if renderer is not None:
            renderer.stop()
        if platform.system() != "Windows":
            loop.remove_signal_handler(signal.SIGINT)
        await session.close()

    if cancelled or had_error:
        return False

    try:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("".join(collected_text), encoding="utf-8")
        console.print(f"\n{_markup(f'✓ Output written to: {output_file}', THEME.success)}")
        return True
    except Exception as exc:
        console.print(f"\n{_markup(f'✗ Failed to write output: {exc}', THEME.error)}")
        return False
