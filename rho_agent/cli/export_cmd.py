"""CLI command: rho-agent export — export sessions to ATIF or OTLP."""

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
    format: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="atif (training trajectories) or otlp (OpenTelemetry spans)",
        ),
    ] = "atif",
    endpoint: Annotated[
        str | None,
        typer.Option(
            "--endpoint",
            help="otlp: send spans to this OTLP/HTTP base URL (/v1/traces is appended). "
            "Headers come from RHO_AGENT_OTLP_HEADERS",
        ),
    ] = None,
    protocol: Annotated[
        str,
        typer.Option("--protocol", help="otlp: http/protobuf (needs the otel extra) or http/json"),
    ] = "http/protobuf",
    include_content: Annotated[
        bool,
        typer.Option(
            "--include-content",
            help="otlp: include prompts, model output, and tool arguments and results",
        ),
    ] = False,
) -> None:
    """Export a session trace as ATIF JSON or OpenTelemetry (OTLP) spans."""
    from ..export.atif import trace_to_atif

    if format not in ("atif", "otlp"):
        console.print(_markup(f"Unknown format {format!r}: use atif or otlp", THEME.error))
        raise typer.Exit(1)
    if format != "otlp" and (endpoint or include_content):
        console.print(_markup("--endpoint and --include-content need --format otlp", THEME.error))
        raise typer.Exit(1)

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

    base_url: str | None = None

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
            base_url = config.base_url
        except Exception:
            pass

    if format == "otlp":
        _export_otlp(
            trace_path,
            session_id=session_id,
            base_url=base_url,
            output=output,
            endpoint=endpoint,
            protocol=protocol,
            include_content=include_content,
        )
        return

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


def _export_otlp(
    trace_path: Path,
    *,
    session_id: str,
    base_url: str | None,
    output: str | None,
    endpoint: str | None,
    protocol: str,
    include_content: bool,
) -> None:
    import os
    import urllib.error

    from ..export.otlp import (
        PROTOCOLS,
        build_request,
        parse_headers,
        provider_name_for,
        send_spans,
        trace_to_spans,
    )
    from ..export.otlp_observer import HEADERS_ENV

    if protocol not in PROTOCOLS:
        console.print(
            _markup(f"Unknown protocol {protocol!r}: use {' or '.join(PROTOCOLS)}", THEME.error)
        )
        raise typer.Exit(1)

    spans = trace_to_spans(
        trace_path,
        session_id=session_id,
        provider_name=provider_name_for(base_url),
        include_content=include_content,
    )

    if endpoint:
        try:
            sent = send_spans(
                spans,
                endpoint,
                headers=parse_headers(os.getenv(HEADERS_ENV)),
                protocol=protocol,
            )
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            console.print(_markup(f"OTLP endpoint returned {e.code}: {detail}", THEME.error))
            raise typer.Exit(1) from e
        except (OSError, ValueError, RuntimeError) as e:
            console.print(_markup(f"OTLP export failed: {e}", THEME.error))
            raise typer.Exit(1) from e
        console.print(_markup(f"Sent {sent} spans to {endpoint}", THEME.success))
        if not output:
            return

    formatted = json.dumps(build_request(spans), indent=2, ensure_ascii=False)
    if output:
        out_path = Path(output).expanduser().resolve()
        out_path.write_text(formatted + "\n", encoding="utf-8")
        console.print(_markup(f"Written to {out_path}", THEME.success))
    else:
        sys.stdout.write(formatted + "\n")
