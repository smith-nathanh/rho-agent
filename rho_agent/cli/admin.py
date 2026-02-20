"""Admin commands: dashboard, ps, kill."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer

from ..control.services.control_plane import ControlPlane
from ..control.services.local_signal_transport import LocalSignalTransport
from ..observability.config import DEFAULT_TELEMETRY_DB
from ..signals import SignalManager
from .theme import THEME
from .formatting import _markup
from .state import app, console


def _default_control_plane() -> tuple[ControlPlane, SignalManager]:
    """Build the default local control plane."""
    sm = SignalManager()
    return ControlPlane(LocalSignalTransport(sm)), sm


@app.command()
def dashboard(
    db_path: Annotated[
        str | None,
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
    control_plane, sm = _default_control_plane()

    if cleanup:
        cleaned = sm.cleanup_stale()
        if cleaned:
            for sid in cleaned:
                console.print(f"[dim]Cleaned: {sid[:8]}[/dim]")
            console.print(_markup(f"Removed {len(cleaned)} stale entries", THEME.success))
        else:
            console.print("[dim]No stale entries found[/dim]")

    agents = control_plane.list_running()
    if not agents:
        console.print("[dim]No running agents[/dim]")
        raise typer.Exit(0)

    now = datetime.now(timezone.utc)
    for agent in agents:
        short_id = agent.session_id[:8]
        try:
            if agent.started_at:
                elapsed = now - agent.started_at.astimezone(timezone.utc)
                secs = int(elapsed.total_seconds())
            else:
                secs = 0
            if secs < 60:
                duration = f"{secs}s"
            elif secs < 3600:
                duration = f"{secs // 60}m{secs % 60}s"
            else:
                duration = f"{secs // 3600}h{(secs % 3600) // 60}m"
        except (ValueError, TypeError):
            duration = "?"
        preview = agent.instruction_preview[:50]
        if len(agent.instruction_preview) > 50:
            preview += "..."
        state = agent.status.value
        state_color = THEME.warning if state == "paused" else THEME.success
        console.print(
            f"  {_markup(short_id, THEME.accent)}  {_markup(state, state_color)}  "
            f"{_markup(f'{agent.model:<14}', THEME.muted)}  {duration:>6}  {preview}"
        )


@app.command()
def kill(
    prefix: Annotated[
        str | None,
        typer.Argument(help="Session ID prefix to kill"),
    ] = None,
    all: Annotated[
        bool,
        typer.Option("--all", help="Kill all running agents"),
    ] = False,
) -> None:
    """Kill running rho-agent sessions by session ID prefix."""
    control_plane, _sm = _default_control_plane()

    target = "all" if all else (prefix or "")
    if not target:
        console.print(_markup("Provide a session ID prefix, or use --all", THEME.error))
        raise typer.Exit(1)

    outcome = control_plane.kill(target)
    if outcome.ok:
        for sid in outcome.acted_session_ids:
            console.print(_markup(f"Cancelled: {sid[:8]}", THEME.warning))
        count = len(outcome.acted_session_ids)
        if all:
            console.print(_markup(f"Sent cancel signal to {count} agents", THEME.success))
    elif outcome.error:
        console.print(_markup(outcome.error, THEME.error))
        raise typer.Exit(1)
    else:
        console.print("[dim]No running agents to kill[/dim]")
