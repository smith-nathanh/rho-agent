"""Display utilities, token tracking, and formatting helpers."""

from __future__ import annotations

import importlib.metadata
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.markup import escape

from ..core.models import limits_for
from .state import console, settings


def _markup(text: str, color: str) -> str:
    """Wrap text in Rich markup with the given color, escaping special chars."""
    return f"[{color}]{escape(text)}[/{color}]"


def _is_interactive_terminal() -> bool:
    """Return True when running in an interactive TTY."""
    return console.is_terminal and sys.stdin.isatty() and sys.stdout.isatty()


def _get_version() -> str:
    """Return the installed package version or 'dev' if not installed."""
    try:
        return importlib.metadata.version("rho-agent")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


def _shorten_path(path: str, working_dir: str | None = None) -> str:
    """Shorten a path relative to ``working_dir`` or the user's home directory."""
    if not path:
        return ""
    try:
        p = Path(path)
        if working_dir:
            wd = Path(working_dir).resolve()
            resolved = p.resolve() if p.is_absolute() else (wd / p).resolve()
            try:
                rel = resolved.relative_to(wd)
                return "." if str(rel) == "." else str(rel)
            except ValueError:
                pass
        home = str(Path.home())
        if path == home or path.startswith(home + os.sep):
            return "~" + path[len(home) :]
    except OSError:
        pass
    return path


def _git_branch(working_dir: str | None = None) -> str | None:
    """Read the current git branch quickly without spawning a subprocess."""
    try:
        cur = Path(working_dir).resolve() if working_dir else Path.cwd()
        for candidate in (cur, *cur.parents):
            dot_git = candidate / ".git"
            if dot_git.is_dir():
                head = (dot_git / "HEAD").read_text(encoding="utf-8").strip()
                break
            if dot_git.is_file():
                # Worktree or submodule: `.git` contains `gitdir: <path>`
                pointer = dot_git.read_text(encoding="utf-8").strip()
                if pointer.startswith("gitdir:"):
                    gitdir = Path(pointer.split(":", 1)[1].strip())
                    if not gitdir.is_absolute():
                        gitdir = (candidate / gitdir).resolve()
                    head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
                    break
        else:
            return None
        if head.startswith("ref: refs/heads/"):
            return head[len("ref: refs/heads/") :]
        if head.startswith("ref: "):
            return head.split("/")[-1]
        return head[:8] if head else None
    except OSError:
        return None


def _format_duration(seconds: float) -> str:
    """Format a float duration cleanly (e.g. '0.4s', '12.3s', '1m 14s')."""
    if seconds < 10:
        return f"{seconds:.1f}s"
    secs = int(seconds)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"


def _format_cost(cost_usd: float) -> str:
    """Format USD cost compactly without noisy trailing zeros."""
    if cost_usd <= 0:
        return ""
    if cost_usd < 0.01:
        return f"${cost_usd:.4f}"
    if cost_usd < 1.0:
        return f"${cost_usd:.3f}"
    return f"${cost_usd:.2f}"


class TokenStatus:
    """Session token metrics for persistent status display."""

    def __init__(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        *,
        model: str = "",
        access_level: str = "workspace",
        context_window: int | None = None,
        cost_usd: float = 0.0,
    ) -> None:
        self.context_size = 0
        self.total_input_tokens = input_tokens
        self.total_output_tokens = output_tokens
        self.total_cached_tokens = 0
        self.total_reasoning_tokens = 0
        self.total_cost_usd = cost_usd
        self.model = model
        self.access_level = access_level
        self.context_window = context_window or limits_for(model).context_window

    def update(self, usage: dict[str, Any] | None) -> None:
        """Update counters from a turn usage dict."""
        if not usage:
            return
        self.context_size = usage.get("context_size", self.context_size)
        self.total_input_tokens = usage.get("total_input_tokens", self.total_input_tokens)
        self.total_output_tokens = usage.get("total_output_tokens", self.total_output_tokens)
        self.total_cached_tokens = usage.get("total_cached_tokens", self.total_cached_tokens)
        self.total_reasoning_tokens = usage.get(
            "total_reasoning_tokens", self.total_reasoning_tokens
        )
        if "total_cost_usd" in usage and usage["total_cost_usd"] is not None:
            self.total_cost_usd = float(usage["total_cost_usd"])

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of input tokens served from prompt cache."""
        if self.total_input_tokens == 0:
            return 0.0
        return self.total_cached_tokens / self.total_input_tokens

    def render(self) -> str:
        """Render token metrics as a compact plain-text status string."""
        cache_pct = f" cache:{self.cache_hit_rate:.0%}" if self.total_cached_tokens else ""
        # Reasoning tokens are part of the output count; shown when there are any.
        reasoning = (
            f" ({self.total_reasoning_tokens} reasoning)" if self.total_reasoning_tokens else ""
        )
        return (
            f"context:{self.context_size}"
            f"{cache_pct}, "
            f"session: {self.total_input_tokens} in | "
            f"{self.total_output_tokens} out{reasoning}"
        )

    def render_toolbar(self) -> list[tuple[str, str]]:
        """Render a quiet, minimal prompt_toolkit bottom status line."""
        parts: list[tuple[str, str]] = [("class:toolbar.muted", " ")]

        pill_cls = {
            "read-only": "class:toolbar.pill.readonly",
            "full": "class:toolbar.pill.full",
        }.get(self.access_level, "class:toolbar.pill.workspace")
        parts.append((pill_cls, self.access_level))

        if self.model:
            parts.append(("class:toolbar.sep", " · "))
            parts.append(("class:toolbar.model", self.model))

        ratio = (
            min(self.context_size / self.context_window, 1.0)
            if self.context_window > 0 and self.context_size > 0
            else 0.0
        )
        ctx_cls = (
            "class:toolbar.err"
            if ratio >= 0.85
            else ("class:toolbar.warn" if ratio >= 0.65 else "class:toolbar.muted")
        )
        parts.append(("class:toolbar.sep", " · "))
        parts.append(
            (
                ctx_cls,
                f"{ratio:.0%} ctx ({_format_token_count(self.context_size)}/{_format_token_count(self.context_window)})",
            )
        )

        if self.total_input_tokens or self.total_output_tokens:
            parts.append(("class:toolbar.sep", " · "))
            parts.append(
                (
                    "class:toolbar.muted",
                    f"{_format_token_count(self.total_input_tokens)} in",
                )
            )
            if self.total_cached_tokens:
                parts.append(("class:toolbar.muted", f" ({self.cache_hit_rate:.0%} cached)"))
            parts.append(
                (
                    "class:toolbar.muted",
                    f" / {_format_token_count(self.total_output_tokens)} out",
                )
            )
            if self.total_reasoning_tokens:
                parts.append(
                    (
                        "class:toolbar.muted",
                        f" ({_format_token_count(self.total_reasoning_tokens)} think)",
                    )
                )

        cost_str = _format_cost(self.total_cost_usd)
        if cost_str:
            parts.append(("class:toolbar.sep", " · "))
            parts.append(("class:toolbar.ok", cost_str))

        parts.append(("class:toolbar.sep", " · "))
        parts.append(("class:toolbar.muted", "/help · exit "))
        return parts


def _sync_token_status_from_state(token_status: TokenStatus, state: Any) -> None:
    """Sync UI token status from State usage dict."""
    token_status.total_input_tokens = state.usage.get("input_tokens", 0)
    token_status.total_output_tokens = state.usage.get("output_tokens", 0)
    token_status.total_cached_tokens = state.usage.get("cached_tokens", 0)
    token_status.total_reasoning_tokens = state.usage.get("reasoning_tokens", 0)
    if "cost_usd" in state.usage:
        token_status.total_cost_usd = float(state.usage.get("cost_usd") or 0.0)


def _format_token_count(tokens: int) -> str:
    """Format a token count as a human-readable string (e.g. '1.2K', '3.4M')."""
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}K"
    return str(tokens)


def _format_elapsed(started_at: datetime, ended_at: datetime | None = None) -> str:
    """Format the elapsed time between two datetimes as a compact string."""
    end = ended_at or datetime.now(UTC)
    elapsed = end - started_at
    secs = max(int(elapsed.total_seconds()), 0)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60}s"
    return f"{secs // 3600}h{(secs % 3600) // 60}m"


def _format_tool_signature(tool_name: str | None, tool_args: dict[str, Any] | None) -> str:
    """Format tool call as a signature like: read(path='/foo/bar.py')"""
    if not tool_name:
        return "()"
    if not tool_args:
        return f"{tool_name}()"

    # Access requests read as what is asked for, and why
    if tool_name == "request_access" and "path" in tool_args:
        path = str(tool_args["path"])
        home = os.path.expanduser("~")
        if path == home or path.startswith(home + os.sep):
            path = "~" + path[len(home) :]
        text = f"{tool_args.get('access', 'write')} access to {path}"
        reason = tool_args.get("reason")
        return f"{text}: {reason}" if reason else text

    # For bash commands, show just the command
    if tool_name == "bash" and "command" in tool_args:
        prefix = "unsandboxed: " if tool_args.get("unsandboxed") else ""
        return f"{tool_name}({prefix}{tool_args['command']})"

    # For edit / write, avoid dumping huge raw strings in plain signatures
    if tool_name == "edit" and "path" in tool_args:
        return f"edit({_shorten_path(str(tool_args['path']))})"
    if tool_name == "write" and "path" in tool_args:
        content = str(tool_args.get("content") or "")
        line_count = content.count("\n") + 1 if content else 0
        return f"write({_shorten_path(str(tool_args['path']))}, {line_count} lines)"

    # For other tools, show compact args
    parts = []
    for key, val in tool_args.items():
        if isinstance(val, str):
            compact = val.replace("\n", "\\n")
            if len(compact) > 80:
                compact = compact[:77] + "..."
            parts.append(f"{key}='{compact}'")
        else:
            parts.append(f"{key}={val}")

    return f"{tool_name}({', '.join(parts)})"


def _tool_succeeded(
    tool_name: str | None,
    metadata: dict[str, Any] | None,
    result: str | None,
) -> bool:
    """Infer whether a completed tool invocation succeeded."""
    if metadata:
        if metadata.get("success") is False:
            return False
        if metadata.get("timed_out"):
            return False
        exit_code = metadata.get("exit_code")
        if exit_code is not None and exit_code != 0:
            return False
        child_status = metadata.get("child_status")
        if isinstance(child_status, str) and child_status in ("error", "cancelled", "dead"):
            return False
    if tool_name == "bash" and result:
        try:
            payload = json.loads(result)
            if isinstance(payload, dict) and payload.get("exit_code", 0) != 0:
                return False
        except json.JSONDecodeError:
            pass
    if result and result.startswith(("Error:", "File not found:", "Not a file:", "Blocked:")):
        return False
    return True


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
                return f"lines {start}–{end} of {total}"

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
                return f"wrote {lines} lines ({size:,} B)"

        # edit tool
        if tool_name == "edit":
            if result and result.startswith("Edited "):
                return "applied"

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
                    return f"timed out ({duration}s)"
                return "timed out"
            if exit_code == 0:
                if duration is not None:
                    return f"exit 0 · {duration}s"
                return "exit 0"
            if exit_code is not None:
                if duration is not None:
                    return f"exit {exit_code} · {duration}s"
                return f"exit {exit_code}"

        # delegate / agent tool
        if tool_name in ("delegate", "agent"):
            child_status = metadata.get("child_status")
            duration = metadata.get("duration_seconds")
            child_id = metadata.get("child_session_id")
            id_part = f" [{child_id}]" if child_id else ""
            if isinstance(child_status, str) and duration is not None:
                return f"subagent{id_part} {child_status} · {duration}s"
            if isinstance(child_status, str):
                return f"subagent{id_part} {child_status}"
            if duration is not None:
                return f"subagent{id_part} finished · {duration}s"
            return f"subagent{id_part} finished"

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

    display_result = display_result.rstrip("\n")
    if not display_result:
        return None

    lines = display_result.split("\n")
    if len(lines) <= max_lines:
        return display_result

    preview_lines = lines[:max_lines]
    remaining = len(lines) - max_lines
    preview_lines.append(f"… {remaining} more lines")
    return "\n".join(preview_lines)


def access_summary(agent: Any) -> str:
    """One line describing what the agent may change, for the CLI."""
    config = agent.config
    if config.backend != "local":
        return "access: full (remote sandbox)"
    access = config.access
    if not access.sandboxed:
        return "access: full · no sandbox"
    sandbox = agent.registry.sandbox
    mechanism = {"seatbelt": "macOS sandbox", "bubblewrap": "bubblewrap"}.get(
        getattr(sandbox, "backend", ""), "sandbox"
    )
    parts = [f"access: {access.level.value}", f"network {'on' if access.network else 'off'}"]
    if access.add_dirs:
        parts.append(f"+{len(access.add_dirs)} dir(s)")
    parts.append(mechanism)
    return " · ".join(parts)
