"""Display utilities, token tracking, and formatting helpers."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import sys
from datetime import datetime, timezone
from typing import Any

import yaml
from rich.markup import escape

from ..runtime import ObservabilityInitializationError
from ..signals import SignalManager
from .theme import THEME
from .state import MARKDOWN_THEME, console, settings


def _markup(text: str, color: str) -> str:
    """Wrap text in Rich markup with the given color, escaping special chars."""
    return f"[{color}]{escape(text)}[/{color}]"


def _is_interactive_terminal() -> bool:
    """Return True when running in an interactive TTY."""
    return console.is_terminal and sys.stdin.isatty() and sys.stdout.isatty()


def _format_observability_init_error(exc: ObservabilityInitializationError) -> str:
    cause = exc.__cause__
    if isinstance(cause, FileNotFoundError):
        return (
            f"Observability initialization failed: {cause}. "
            "Fix --observability-config to point to an existing YAML file and retry."
        )

    if isinstance(cause, yaml.YAMLError):
        config_location = exc.config_path or "the observability config file"
        return (
            f"Observability initialization failed: invalid YAML in {config_location}: "
            f"{cause}. Fix the YAML syntax and retry."
        )

    if isinstance(cause, ValueError) and "tenant" in str(cause).lower():
        return (
            "Observability initialization failed: missing tenant information. "
            "Set both --team-id and --project-id, or define "
            "observability.tenant.team_id and observability.tenant.project_id in "
            "the observability config."
        )

    guidance = []
    if exc.config_path:
        guidance.append("verify --observability-config points to a valid YAML file")
    if exc.team_id and not exc.project_id:
        guidance.append("provide --project-id")
    if exc.project_id and not exc.team_id:
        guidance.append("provide --team-id")
    if not guidance:
        guidance.append("set both --team-id and --project-id when observability is enabled")

    return f"Observability initialization failed: {cause or exc}. To fix: {'; '.join(guidance)}."


def _get_version() -> str:
    """Return the installed package version or 'dev' if not installed."""
    try:
        return importlib.metadata.version("rho-agent")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


class TokenStatus:
    """Session token metrics for persistent status display."""

    def __init__(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.context_size = 0
        self.total_input_tokens = input_tokens
        self.total_output_tokens = output_tokens

    def update(self, usage: dict[str, int] | None) -> None:
        """Update counters from a turn usage dict."""
        if not usage:
            return
        self.context_size = usage.get("context_size", self.context_size)
        self.total_input_tokens = usage.get("total_input_tokens", self.total_input_tokens)
        self.total_output_tokens = usage.get("total_output_tokens", self.total_output_tokens)

    def render(self) -> str:
        """Render token metrics as a compact status-bar string."""
        return (
            f"context:{self.context_size} "
            f"in:{self.total_input_tokens} "
            f"out:{self.total_output_tokens}"
        )


def _sync_token_status_from_session(token_status: TokenStatus, session: Any) -> None:
    """Sync UI token status from current session counters."""
    token_status.context_size = session.last_input_tokens
    token_status.total_input_tokens = session.total_input_tokens
    token_status.total_output_tokens = session.total_output_tokens


def _format_token_count(tokens: int) -> str:
    """Format a token count as a human-readable string (e.g. '1.2K', '3.4M')."""
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}K"
    return str(tokens)


def _format_elapsed(started_at: datetime, ended_at: datetime | None = None) -> str:
    """Format the elapsed time between two datetimes as a compact string."""
    end = ended_at or datetime.now(timezone.utc)
    elapsed = end - started_at
    secs = int(elapsed.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60}s"
    return f"{secs // 3600}h{(secs % 3600) // 60}m"


async def _wait_while_paused(signal_manager: SignalManager, session_id: str) -> bool:
    """Block at a turn boundary while paused; return False if externally cancelled."""
    announced = False
    while signal_manager.is_paused(session_id):
        if signal_manager.is_cancelled(session_id):
            return False
        if not announced:
            console.print(
                _markup("Paused by rho-agent monitor; waiting for resume...", THEME.warning)
            )
            announced = True
        await asyncio.sleep(0.5)
    if announced:
        console.print(_markup("Resumed by rho-agent monitor", THEME.success))
    return True


def _format_tool_signature(tool_name: str | None, tool_args: dict[str, Any] | None) -> str:
    """Format tool call as a signature like: read(path='/foo/bar.py')"""
    if not tool_name:
        return "()"
    if not tool_args:
        return f"{tool_name}()"

    # For bash commands, show just the command
    if tool_name == "bash" and "command" in tool_args:
        return f"{tool_name}({tool_args['command']})"

    # For other tools, show all args (no truncation)
    parts = []
    for key, val in tool_args.items():
        if isinstance(val, str):
            parts.append(f"{key}='{val}'")
        else:
            parts.append(f"{key}={val}")

    return f"{tool_name}({', '.join(parts)})"


def _format_tool_summary(
    tool_name: str | None,
    metadata: dict[str, Any] | None,
    result: str | None,
) -> str | None:
    """Format a brief summary of tool results."""
    if not tool_name:
        return None

    # Use metadata if available
    if metadata:
        # grep tool
        if tool_name == "grep":
            matches = metadata.get("matches", 0)
            truncated = metadata.get("truncated", False)
            if matches:
                suffix = "+" if truncated else ""
                return f"{matches}{suffix} matches"
            return "No matches"

        # read tool
        if tool_name == "read":
            total = metadata.get("total_lines", 0)
            start = metadata.get("start_line", 1)
            end = metadata.get("end_line", total)
            if total:
                return f"Read lines {start}-{end} of {total}"

        # list tool
        if tool_name == "list":
            count = metadata.get("item_count", 0)
            if count:
                return f"{count} items"

        # write tool
        if tool_name == "write":
            size = metadata.get("size_bytes", 0)
            lines = metadata.get("lines", 0)
            if size:
                return f"Wrote {size} bytes ({lines} lines)"

        # glob tool
        if tool_name == "glob":
            matches = metadata.get("matches", 0)
            total = metadata.get("total", matches)
            if matches:
                if total > matches:
                    return f"{matches} of {total} files"
                return f"{matches} files"
            return "No files found"

        # Database tools
        if tool_name in ("oracle", "sqlite", "vertica", "mysql", "postgres"):
            rows = metadata.get("row_count", metadata.get("table_count", 0))
            if rows:
                return f"{rows} rows"

        # bash tool
        if tool_name == "bash":
            timed_out = metadata.get("timed_out", False)
            duration = metadata.get("duration_seconds")
            exit_code = metadata.get("exit_code")
            if timed_out:
                if duration is not None:
                    return f"Command timed out ({duration}s)"
                return "Command timed out"
            if exit_code == 0:
                if duration is not None:
                    return f"Command succeeded ({duration}s)"
                return "Command succeeded"
            if exit_code is not None:
                if duration is not None:
                    return f"Command failed (exit {exit_code}, {duration}s)"
                return f"Command failed (exit {exit_code})"

        # delegate tool
        if tool_name == "delegate":
            child_status = metadata.get("child_status")
            duration = metadata.get("duration_seconds")
            if isinstance(child_status, str) and duration is not None:
                return f"Sub-agent {child_status} ({duration}s)"
            if isinstance(child_status, str):
                return f"Sub-agent {child_status}"
            if duration is not None:
                return f"Sub-agent finished ({duration}s)"
            return "Sub-agent finished"

    # Fallback: count lines in result
    if result:
        lines = result.count("\n") + 1
        if lines > 1:
            return f"{lines} lines"

    return None


def _format_tool_preview(
    result: str | None,
    tool_name: str | None = None,
    max_lines: int | None = None,
) -> str | None:
    """Get first N lines of tool output as a preview."""
    if max_lines is None:
        max_lines = settings.tool_preview_lines

    if not result or max_lines <= 0:
        return None

    display_result = result

    # Bash returns a JSON wrapper; show command output body in interactive preview.
    if tool_name == "bash":
        try:
            payload = json.loads(result)
            if isinstance(payload, dict):
                output = payload.get("output")
                if isinstance(output, str):
                    display_result = output
        except json.JSONDecodeError:
            pass

    lines = display_result.split("\n")
    if len(lines) <= max_lines:
        return display_result

    preview_lines = lines[:max_lines]
    remaining = len(lines) - max_lines
    preview_lines.append(f"... ({remaining} more lines)")
    return "\n".join(preview_lines)
