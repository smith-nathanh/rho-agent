"""CLI command: rho-agent export — export sessions to ATIF format."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from .formatting import _markup
from .state import app, console
from .theme import THEME


@app.command()
def export(
    session_id: Annotated[
        str,
        typer.Argument(help="Session ID (directory name) to export"),
    ],
    dir: Annotated[
        str,
        typer.Option("--dir", "-d", help="Sessions directory"),
    ] = "~/.config/rho-agent/sessions",
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Output file path (default: stdout)"),
    ] = None,
) -> None:
    """Export a session trace as ATIF JSON."""
    from ..export.atif import trace_to_atif

    sessions_dir = Path(dir).expanduser().resolve()
    session_dir = sessions_dir / session_id
    if not session_dir.is_dir():
        console.print(_markup(f"Session not found: {session_dir}", THEME.error))
        raise typer.Exit(1)

    trace_path = session_dir / "trace.jsonl"
    if not trace_path.exists():
        console.print(_markup(f"No trace.jsonl in {session_dir}", THEME.error))
        raise typer.Exit(1)

    # Read metadata from meta.json and config.yaml
    agent_name = "rho-agent"
    agent_version = "0.1.0"
    model_name: str | None = None

    meta_path = session_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        model_name = meta.get("model")

    config_path = session_dir / "config.yaml"
    if config_path.exists():
        try:
            from ..core.config import AgentConfig

            config = AgentConfig.from_file(config_path)
            model_name = model_name or config.model
        except Exception:
            pass

    try:
        import importlib.metadata

        agent_version = importlib.metadata.version("rho-agent")
    except Exception:
        pass

    trajectory = trace_to_atif(
        trace_path,
        session_id=session_id,
        agent_name=agent_name,
        agent_version=agent_version,
        model_name=model_name,
    )

    formatted = json.dumps(trajectory, indent=2, ensure_ascii=False)

    if output:
        out_path = Path(output).expanduser().resolve()
        out_path.write_text(formatted + "\n", encoding="utf-8")
        console.print(_markup(f"Written to {out_path}", THEME.success))
    else:
        sys.stdout.write(formatted + "\n")
