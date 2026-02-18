"""Admin commands: dashboard, ps, kill."""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import typer

from ..observability.config import DEFAULT_TELEMETRY_DB
from ..signals import SignalManager
from ..ui.theme import THEME
from .formatting import _markup
from .state import app, console


@app.command()
def dashboard(
    db_path: Annotated[
        Optional[str],
        typer.Option("--db", help="Path to telemetry database"),
    ] = None,
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Port to run dashboard on"),
    ] = 8501,
) -> None:
    """Launch the observability dashboard."""
    import subprocess
    import sys

    try:
        import streamlit  # noqa: F401
    except ImportError:
        console.print(
            _markup(
                "Streamlit not installed. Install with: pip install 'rho-agent[dashboard]'",
                THEME.error,
            )
        )
        raise typer.Exit(1)

    # Set database path in environment
    resolved_db = db_path or str(DEFAULT_TELEMETRY_DB)
    env = os.environ.copy()
    env["RHO_AGENT_TELEMETRY_DB"] = resolved_db

    # Get path to dashboard app
    dashboard_path = Path(__file__).parent.parent / "observability" / "dashboard" / "app.py"

    if not dashboard_path.exists():
        console.print(_markup(f"Dashboard app not found at {dashboard_path}", THEME.error))
        raise typer.Exit(1)

    console.print(_markup(f"Starting dashboard on port {port}...", THEME.success))
    console.print(_markup(f"Database: {resolved_db}", THEME.muted))
    console.print(_markup(f"Open http://localhost:{port} in your browser", THEME.muted))

    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(dashboard_path),
                "--server.port",
                str(port),
                "--server.headless",
                "true",
            ],
            env=env,
            check=True,
        )
    except KeyboardInterrupt:
        console.print(_markup("\nDashboard stopped", THEME.muted))
    except subprocess.CalledProcessError as e:
        console.print(_markup(f"Dashboard failed to start: {e}", THEME.error))
        raise typer.Exit(1) from e


@app.command()
def ps(
    cleanup: Annotated[
        bool,
        typer.Option("--cleanup", help="Remove stale entries from crashed agents"),
    ] = False,
) -> None:
    """List running rho-agent sessions."""
    sm = SignalManager()

    if cleanup:
        cleaned = sm.cleanup_stale()
        if cleaned:
            for sid in cleaned:
                console.print(f"[dim]Cleaned: {sid[:8]}[/dim]")
            console.print(_markup(f"Removed {len(cleaned)} stale entries", THEME.success))
        else:
            console.print("[dim]No stale entries found[/dim]")

    agents = sm.list_running()
    if not agents:
        console.print("[dim]No running agents[/dim]")
        raise typer.Exit(0)

    now = datetime.now(timezone.utc)
    for info in agents:
        short_id = info.session_id[:8]
        paused = sm.is_paused(info.session_id)
        try:
            started = datetime.fromisoformat(info.started_at)
            elapsed = now - started
            secs = int(elapsed.total_seconds())
            if secs < 60:
                duration = f"{secs}s"
            elif secs < 3600:
                duration = f"{secs // 60}m{secs % 60}s"
            else:
                duration = f"{secs // 3600}h{(secs % 3600) // 60}m"
        except ValueError:
            duration = "?"
        preview = info.instruction_preview[:50]
        if len(info.instruction_preview) > 50:
            preview += "..."
        state = "paused" if paused else "running"
        state_color = THEME.warning if paused else THEME.success
        console.print(
            f"  {_markup(short_id, THEME.accent)}  {_markup(state, state_color)}  "
            f"{_markup(f'{info.model:<14}', THEME.muted)}  {duration:>6}  {preview}"
        )


@app.command()
def kill(
    prefix: Annotated[
        Optional[str],
        typer.Argument(help="Session ID prefix to kill"),
    ] = None,
    all: Annotated[
        bool,
        typer.Option("--all", help="Kill all running agents"),
    ] = False,
) -> None:
    """Kill running rho-agent sessions by session ID prefix."""
    sm = SignalManager()

    if all:
        cancelled = sm.cancel_all()
        if cancelled:
            for sid in cancelled:
                console.print(_markup(f"Cancelled: {sid[:8]}", THEME.warning))
            console.print(_markup(f"Sent cancel signal to {len(cancelled)} agents", THEME.success))
        else:
            console.print("[dim]No running agents to kill[/dim]")
        return

    if not prefix:
        console.print(_markup("Provide a session ID prefix, or use --all", THEME.error))
        raise typer.Exit(1)

    cancelled = sm.cancel_by_prefix(prefix)
    if cancelled:
        for sid in cancelled:
            console.print(_markup(f"Cancelled: {sid[:8]}", THEME.warning))
    else:
        console.print(_markup(f"No running agents matching prefix '{prefix}'", THEME.error))
        raise typer.Exit(1)
