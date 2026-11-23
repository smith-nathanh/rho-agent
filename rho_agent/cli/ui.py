"""Minimal, modern visual components and live turn renderer for the rho-agent CLI."""

from __future__ import annotations

import asyncio
import difflib
import re
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.live import Live
from rich.markdown import Heading, Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from ..core.events import AgentEvent
from ..core.models import limits_for
from .formatting import (
    TokenStatus,
    _format_cost,
    _format_duration,
    _format_elapsed,
    _format_token_count,
    _format_tool_preview,
    _format_tool_signature,
    _format_tool_summary,
    _git_branch,
    _markup,
    _shorten_path,
    _tool_succeeded,
    access_summary,
)
from .state import console as default_console
from .theme import THEME, THEMES, status_style, tool_visual

# Clean subtle pulse dots (modern, non-retro)
_SPINNER_FRAMES = ("●", "●", "◉", "○", "◉")
_MAX_DIFF_LINES = 18

# Exploration tools whose raw output is verbose; show a compact 1-line summary on success
_COMPACT_TOOLS = frozenset({"read", "list", "glob"})
_HAS_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?s\b")


class _MinimalHeading(Heading):
    """Left-aligned architectural Markdown heading (replaces Rich's centered H1/H2)."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        text = self.text
        text.justify = "left"
        if self.tag == "h1":
            text.stylize(f"bold {THEME.brand}")
            yield text
            yield Rule(style=THEME.border)
        elif self.tag == "h2":
            text.stylize(f"bold {THEME.primary}")
            yield text
            yield Rule(style=THEME.border)
        else:
            text.stylize(f"bold {THEME.secondary}")
            yield text


class MinimalMarkdown(Markdown):
    """Markdown renderer with clean left-aligned headings and subtle hairlines."""

    elements: ClassVar[dict[str, Any]] = {
        **Markdown.elements,
        "heading_open": _MinimalHeading,
    }


def make_markdown(text: str) -> MinimalMarkdown:
    """Create a minimal left-aligned Markdown renderable."""
    code_theme = "ansi_light" if THEME.name == "light" else "ansi_dark"
    return MinimalMarkdown(
        text,
        style=THEME.primary,
        code_theme=code_theme,
        inline_code_theme=code_theme,
    )


def _access_label(level: str, *, backend: str = "local") -> Text:
    """Clean text label for sandbox access level."""
    if backend != "local":
        return Text("remote sandbox", style=THEME.secondary)
    if level == "read-only":
        return Text("read-only", style=THEME.secondary)
    if level == "full":
        return Text("full (no sandbox)", style=THEME.warning)
    return Text("workspace", style=THEME.primary)


def render_user_prompt(text: str, *, label: str | None = None) -> RenderableType:
    """Render a user prompt as a distinct matte-charcoal strip with a subtle left bar."""
    bg = THEME.user_bg
    grid = Table.grid(expand=True, padding=0)
    grid.add_column(width=2, no_wrap=True)
    grid.add_column(ratio=1)

    lines = text.splitlines() or [""]
    if label:
        grid.add_row(
            Text("┃ ", style=f"{THEME.mark} on {bg}"),
            Text(label, style=f"{THEME.muted} on {bg}"),
            style=f"on {bg}",
        )
    for line in lines:
        grid.add_row(
            Text("┃ ", style=f"{THEME.mark} on {bg}"),
            Text(line, style=f"bold {THEME.brand} on {bg}"),
            style=f"on {bg}",
        )
    return grid


def render_agent_divider(*, after_tools: bool = False) -> RenderableType:
    """Subtle visual marker separating the agent's voice from user prompts and tool work."""
    if after_tools:
        return Rule(
            title=f"[bold {THEME.mark}]ρ[/bold {THEME.mark}]",
            align="left",
            style=THEME.border,
        )
    return Text("ρ", style=f"bold {THEME.mark}")


def render_welcome_banner(
    session: Any,
    working_dir: str,
    version: str,
) -> RenderableType:
    """Clean, unboxed editorial header bounded by whisper-thin hairlines."""
    config = session.agent.config
    branch = _git_branch(working_dir)
    proj_display = _shorten_path(working_dir) or "."
    level = config.access.level.value if config.backend == "local" else "remote"

    top = Table.grid(expand=True)
    top.add_column(ratio=1)
    top.add_column(justify="right", no_wrap=True)

    left_title = Text()
    left_title.append("ρ ", style=f"bold {THEME.mark}")
    left_title.append("rho-agent", style=f"bold {THEME.brand}")
    left_title.append(f"  {version}", style=THEME.muted)

    right_id = Text()
    if getattr(session, "id", None):
        right_id.append(f"#{session.id}", style=THEME.muted)
    top.add_row(left_title, right_id)

    meta = Text()
    meta.append(config.model or "default", style=THEME.secondary)
    if getattr(config, "reasoning_effort", None):
        meta.append(f" ({config.reasoning_effort})", style=THEME.muted)
    meta.append("  ·  ", style=THEME.border_active)
    meta.append_text(_access_label(level, backend=config.backend))
    if config.backend == "local" and config.access.sandboxed:
        sandbox = getattr(session.agent.registry, "sandbox", None)
        mech = {"seatbelt": "seatbelt", "bubblewrap": "bwrap"}.get(
            getattr(sandbox, "backend", ""), "sandbox"
        )
        net = "" if config.access.network else ", offline"
        extras = f", +{len(config.access.add_dirs)} dir" if config.access.add_dirs else ""
        meta.append(f" ({mech}{net}{extras})", style=THEME.muted)

    proj = Text()
    proj.append(proj_display, style=THEME.muted)
    if branch:
        proj.append("  (", style=THEME.border_active)
        proj.append(branch, style=THEME.subtle)
        proj.append(")", style=THEME.border_active)

    bottom = Table.grid(expand=True)
    bottom.add_column(ratio=1)
    bottom.add_column(justify="right", no_wrap=True)
    hints = Text("/help · /status · exit", style=THEME.muted)
    bottom.add_row(proj, hints)

    return Group(
        Rule(style=THEME.border),
        top,
        meta,
        bottom,
        Rule(style=THEME.border),
    )


def render_single_header(session: Any, working_dir: str) -> RenderableType:
    """Ultra-minimal single-line header for one-shot runs."""
    config = session.agent.config
    level = config.access.level.value if config.backend == "local" else "remote"
    line = Text()
    line.append("ρ ", style=f"bold {THEME.accent}")
    line.append("rho-agent", style=f"bold {THEME.brand}")
    line.append("  ·  ", style=THEME.border_active)
    line.append(config.model or "", style=THEME.secondary)
    line.append("  ·  ", style=THEME.border_active)
    line.append_text(_access_label(level, backend=config.backend))
    branch = _git_branch(working_dir)
    if branch:
        line.append(f"  ·  {branch}", style=THEME.muted)
    if getattr(session, "id", None):
        line.append(f"  #{session.id}", style=THEME.muted)
    return Group(line, Rule(style=THEME.border))


def _build_edit_diff(old_string: str, new_string: str) -> tuple[list[Text], int, int]:
    """Compute a clean editorial unified diff between ``old_string`` and ``new_string``."""
    old_lines = old_string.splitlines()
    new_lines = new_string.splitlines()
    raw = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            lineterm="",
            n=2,
        )
    )
    diff_body = [ln for ln in raw if not ln.startswith(("---", "+++"))]
    added = sum(1 for ln in diff_body if ln.startswith("+"))
    removed = sum(1 for ln in diff_body if ln.startswith("-"))

    rendered: list[Text] = []
    shown = diff_body[:_MAX_DIFF_LINES]
    for ln in shown:
        row = Text()
        row.append("  │ ", style=THEME.rail)
        if ln.startswith("@@"):
            row.append(ln, style=THEME.diff_hunk)
        elif ln.startswith("+"):
            row.append(ln, style=f"{THEME.diff_add} on {THEME.diff_add_bg}")
        elif ln.startswith("-"):
            row.append(ln, style=f"{THEME.diff_del} on {THEME.diff_del_bg}")
        else:
            row.append(ln, style=THEME.muted)
        rendered.append(row)

    remaining = len(diff_body) - len(shown)
    if remaining > 0:
        more = Text()
        more.append("  │ ", style=THEME.rail)
        more.append(f"… {remaining} more lines", style=THEME.muted)
        rendered.append(more)

    return rendered, added, removed


def _compact_arg_target(
    tool_name: str | None,
    tool_args: dict[str, Any] | None,
    working_dir: str | None = None,
) -> tuple[str, str]:
    """Return ``(primary_target, secondary_detail)`` for a tool call header."""
    if not tool_args:
        return "", ""

    if tool_name == "bash":
        cmd = str(tool_args.get("command") or "").strip()
        first_line = cmd.splitlines()[0] if cmd else ""
        if len(cmd.splitlines()) > 1:
            first_line = f"{first_line}  (+{len(cmd.splitlines()) - 1} lines)"
        tag = "unsandboxed" if tool_args.get("unsandboxed") else ""
        return first_line, tag

    if tool_name in ("read", "view_image", "read_excel"):
        path = _shorten_path(str(tool_args.get("path") or ""), working_dir)
        offset = tool_args.get("offset")
        limit = tool_args.get("limit")
        detail = ""
        if offset is not None and limit is not None:
            try:
                detail = f"{int(offset)}–{int(offset) + int(limit) - 1}"
            except (TypeError, ValueError):
                detail = f"{offset}+{limit}"
        elif offset is not None:
            detail = f"from {offset}"
        elif limit is not None:
            detail = f"first {limit} lines"
        return path, detail

    if tool_name == "edit":
        return _shorten_path(str(tool_args.get("path") or ""), working_dir), ""

    if tool_name == "write":
        path = _shorten_path(str(tool_args.get("path") or ""), working_dir)
        content = str(tool_args.get("content") or "")
        lines = content.count("\n") + 1 if content else 0
        return path, f"{lines} lines"

    if tool_name == "grep":
        pattern = str(tool_args.get("pattern") or "")
        path = _shorten_path(str(tool_args.get("path") or "."), working_dir)
        return f'"{pattern}"', path

    if tool_name == "glob":
        pattern = str(tool_args.get("pattern") or "")
        path = _shorten_path(str(tool_args.get("path") or "."), working_dir)
        return pattern, path if path and path != "." else ""

    if tool_name == "list":
        return _shorten_path(str(tool_args.get("path") or "."), working_dir), ""

    if tool_name in ("delegate", "agent"):
        instruction = str(
            tool_args.get("instruction") or tool_args.get("prompt") or tool_args.get("task") or ""
        ).strip()
        one_line = " ".join(instruction.split())
        if len(one_line) > 84:
            one_line = one_line[:81] + "…"
        return one_line, str(tool_args.get("model") or "")

    if tool_name == "request_access":
        path = _shorten_path(str(tool_args.get("path") or ""), working_dir)
        access = str(tool_args.get("access") or "write")
        reason = str(tool_args.get("reason") or "")
        return f"{access} {path}", reason

    if tool_name in ("sqlite", "postgres", "mysql", "oracle", "vertica"):
        query = str(tool_args.get("query") or tool_args.get("sql") or "").strip()
        one_line = " ".join(query.split())
        if len(one_line) > 88:
            one_line = one_line[:85] + "…"
        return one_line, ""

    parts = []
    for k, v in tool_args.items():
        val = str(v).replace("\n", " ")
        if len(val) > 48:
            val = val[:45] + "…"
        parts.append(f"{k}={val}")
    return ", ".join(parts), ""


def render_tool_start(
    tool_name: str | None,
    tool_args: dict[str, Any] | None,
    *,
    working_dir: str | None = None,
) -> RenderableType:
    """Render a quiet, recessed orthogonal-rail header for a tool call."""
    marker, _ = tool_visual(tool_name)
    name = tool_name or "tool"
    target, detail = _compact_arg_target(tool_name, tool_args, working_dir)

    header = Text()
    header.append(f"  {marker} ", style=THEME.accent if marker == "↳" else THEME.border_active)
    header.append(name, style=THEME.secondary)
    if target:
        header.append("  ", style="")
        header.append(target, style=THEME.subtle)
    if detail:
        detail_style = THEME.warning if detail == "unsandboxed" else THEME.muted
        header.append(f"  {detail}", style=detail_style)

    if tool_name == "edit" and tool_args:
        old_s = tool_args.get("old_string")
        new_s = tool_args.get("new_string")
        if isinstance(old_s, str) and isinstance(new_s, str) and (old_s or new_s):
            diff_rows, added, removed = _build_edit_diff(old_s, new_s)
            if added or removed:
                header.append("  ", style="")
                if added:
                    header.append(f"+{added}", style=THEME.diff_add)
                if added and removed:
                    header.append(" ", style="")
                if removed:
                    header.append(f"-{removed}", style=THEME.diff_del)
            if diff_rows:
                return Group(header, *diff_rows)

    return header


def render_tool_end(
    tool_name: str | None,
    metadata: dict[str, Any] | None,
    result: str | None,
    *,
    elapsed: float | None = None,
) -> RenderableType | None:
    """Render a quiet rail footer and folded output preview for a finished tool."""
    ok = _tool_succeeded(tool_name, metadata, result)
    summary = _format_tool_summary(tool_name, metadata, result)

    # Keep exploration tools (read/list/glob) and successful edits compact so they
    # don't bury the conversation under raw line-numbered file/directory dumps.
    suppress_preview = ok and (tool_name == "edit" or tool_name in _COMPACT_TOOLS)
    preview = None if suppress_preview else _format_tool_preview(result, tool_name)

    error_text = summary or (result.splitlines()[0][:90] if result else "failed")
    # Avoid printing the exact same single-line error in both preview and footer
    if not ok and preview and preview.strip() == error_text.strip():
        preview = None

    if not summary and not preview and ok:
        return None

    rows: list[Text] = []
    if preview:
        for line in preview.splitlines():
            row = Text()
            row.append("  │ ", style=THEME.rail)
            is_more = line.startswith("… ") and line.endswith(" more lines")
            row.append(line, style=THEME.muted if is_more else THEME.tool_result)
            rows.append(row)

    status_row = Text()
    status_row.append("  └ ", style=THEME.rail)
    if ok:
        status_row.append(summary or "done", style=THEME.muted)
    else:
        status_row.append(f"× {error_text}", style=THEME.error)

    has_duration = bool(summary and _HAS_DURATION_RE.search(summary))
    if elapsed is not None and elapsed >= 0.05 and not has_duration:
        status_row.append(f"  {_format_duration(elapsed)}", style=THEME.muted)

    rows.append(status_row)
    return Group(*rows)


def render_tool_block(
    tool_name: str | None,
    tool_args: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    result: str | None,
    *,
    working_dir: str | None = None,
    elapsed: float | None = None,
    blocked: bool = False,
) -> RenderableType:
    """Render a complete tool call + result atomically so parallel tools never interleave."""
    ok = (not blocked) and _tool_succeeded(tool_name, metadata, result)
    summary = _format_tool_summary(tool_name, metadata, result)

    # Ultra-compact single-line row for clean successful read/list/glob calls
    if ok and tool_name in _COMPACT_TOOLS:
        target, detail = _compact_arg_target(tool_name, tool_args, working_dir)
        row = Text()
        row.append("  │ ", style=THEME.rail)
        row.append(f"{tool_name:<5}", style=THEME.secondary)
        if target:
            row.append(f" {target}", style=THEME.subtle)
        if detail:
            row.append(f"  {detail}", style=THEME.muted)
        if summary:
            row.append(f"  ·  {summary}", style=THEME.muted)
        if elapsed is not None and elapsed >= 0.2:
            row.append(f"  {_format_duration(elapsed)}", style=THEME.muted)
        return row

    header = render_tool_start(tool_name, tool_args, working_dir=working_dir)
    if blocked:
        footer = Text()
        footer.append("  └ ", style=THEME.rail)
        footer.append("rejected by user", style=THEME.warning)
        return Group(header, footer)

    tail = render_tool_end(tool_name, metadata, result, elapsed=elapsed)
    if tail is None:
        return header
    return Group(header, tail)


def render_approval_card(
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    request_id: str | None = None,
    working_dir: str | None = None,
) -> tuple[Panel, str]:
    """Build a crisp 1px square hairline approval card."""
    reason = str(tool_args.get("reason") or "").strip()
    high_risk = False
    warning_text = ""

    if tool_name == "request_access":
        path = str(tool_args.get("path", ""))
        kind = "read" if tool_args.get("access") == "read" else "write"
        if path in ("/", str(Path.home())):
            high_risk = True
            whole = "filesystem" if path == "/" else "home directory"
            warning_text = f"Caution: grants access to your entire {whole}."
        title = f"Access request · {kind} outside sandbox"
        target_label = "path"
        target_val = _shorten_path(path, working_dir) or path
        question = "Allow for this session? [y/N]"
    elif tool_name == "bash" and tool_args.get("unsandboxed"):
        high_risk = True
        title = "Unsandboxed command"
        target_label = "command"
        target_val = str(tool_args.get("command", ""))
        question = "Run outside sandbox? [y/N]"
    else:
        title = f"Approval required · {tool_name}"
        target_label = "call"
        target_val = _format_tool_signature(tool_name, tool_args)
        question = "Approve tool call? [y/N]"

    if request_id:
        title = f"{title}  [{request_id}]"

    border = THEME.error if high_risk else THEME.border_active
    dot_color = THEME.error if high_risk else THEME.warning
    title_markup = (
        f"[{THEME.error}]{escape(title)}[/{THEME.error}]"
        if high_risk
        else f"[{dot_color}]●[/{dot_color}] [bold {THEME.brand}]{escape(title)}[/bold {THEME.brand}]"
    )
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=THEME.muted, no_wrap=True)
    grid.add_column()
    grid.add_row(target_label, Text(target_val, style=THEME.primary))
    if reason:
        grid.add_row("reason", Text(reason, style=THEME.subtle))

    items: list[RenderableType] = [grid]
    if warning_text:
        items.append(Text(""))
        items.append(Text(warning_text, style=THEME.error))

    panel = Panel(
        Group(*items),
        title=title_markup,
        title_align="left",
        box=box.SQUARE,
        border_style=border,
        padding=(0, 1),
    )
    return panel, question


def render_turn_footer(
    usage: dict[str, Any] | None,
    *,
    model: str = "",
    elapsed: float | None = None,
    context_window: int | None = None,
) -> RenderableType:
    """Quiet single-line telemetry footer after a turn."""
    u = usage or {}
    ctx = int(u.get("context_size", 0) or 0)
    total_in = int(u.get("total_input_tokens", 0) or 0)
    total_out = int(u.get("total_output_tokens", 0) or 0)
    cached = int(u.get("total_cached_tokens", 0) or 0)
    reasoning = int(u.get("total_reasoning_tokens", 0) or 0)
    cost = float(u.get("total_cost_usd", 0.0) or 0.0)
    window = context_window or limits_for(model).context_window
    ratio = min(ctx / window, 1.0) if window > 0 and ctx > 0 else 0.0

    line = Text()
    ctx_color = THEME.error if ratio >= 0.85 else (THEME.warning if ratio >= 0.65 else THEME.muted)
    line.append(f"{ratio:.0%} ctx ({_format_token_count(ctx)})", style=ctx_color)
    line.append("  ·  ", style=THEME.border_active)
    line.append(f"{_format_token_count(total_in)} in", style=THEME.muted)
    if cached and total_in:
        line.append(f" ({cached / total_in:.0%} cached)", style=THEME.muted)
    line.append(f" / {_format_token_count(total_out)} out", style=THEME.muted)
    if reasoning:
        line.append(f" ({_format_token_count(reasoning)} think)", style=THEME.muted)
    cost_str = _format_cost(cost)
    if cost_str:
        line.append("  ·  ", style=THEME.border_active)
        line.append(cost_str, style=THEME.secondary)
    if elapsed is not None:
        line.append("  ·  ", style=THEME.border_active)
        line.append(_format_duration(elapsed), style=THEME.muted)
    return Group(Rule(style=THEME.border), line)


def render_help_card() -> Panel:
    """Minimal categorized /help reference card."""
    sections: list[tuple[str, list[tuple[str, str]]]] = [
        (
            "Session",
            [
                ("/status", "Context window, token usage, cache rate & cost"),
                ("/access [level]", "Inspect sandbox or switch: read-only | workspace | full"),
                ("/access revoke <path>", "Remove an extra directory grant"),
                ("/compact [guidance]", "Compact conversation history"),
                ("/resume [latest|id]", "Switch to a saved session"),
                ("/download <src> <dst>", "Download a file from a remote sandbox"),
                ("/theme [name]", "List or switch CLI color themes"),
                ("/clear", "Clear screen"),
                ("exit / quit", "Save and exit"),
            ],
        ),
        (
            "Keys",
            [
                ("Enter", "Send message"),
                ("Esc + Enter", "Newline"),
                ("Tab", "Complete commands and file paths"),
                ("Ctrl + C", "Cancel current turn"),
            ],
        ),
        (
            "Fleet & History",
            [
                ("rho-agent monitor", "Open monitor command shell"),
                ("rho-agent monitor --live", "Open live Mission Control view"),
                ("rho-agent ps", "List sessions by project"),
                ("rho-agent -r latest", "Resume latest session"),
            ],
        ),
    ]

    blocks: list[RenderableType] = []
    for idx, (heading, rows) in enumerate(sections):
        if idx:
            blocks.append(Text(""))
        blocks.append(Text(heading, style=f"bold {THEME.primary}"))
        grid = Table.grid(padding=(0, 3))
        grid.add_column(width=24, style=THEME.secondary, no_wrap=True)
        grid.add_column(style=THEME.muted)
        for lhs, rhs in rows:
            grid.add_row(f"  {lhs}", rhs)
        blocks.append(grid)

    return Panel(
        Group(*blocks),
        title=f"[{THEME.secondary}]Help[/{THEME.secondary}]",
        title_align="left",
        box=box.SQUARE,
        border_style=THEME.border,
        padding=(0, 1),
    )


def render_themes_card(*, command_prefix: str = "/theme") -> Panel:
    """Render available CLI/monitor themes with live color swatches."""
    table = Table(
        show_header=True,
        header_style=THEME.muted,
        box=box.SIMPLE_HEAD,
        border_style=THEME.border,
        expand=True,
        pad_edge=False,
    )
    table.add_column("Theme", no_wrap=True)
    table.add_column("Swatches", no_wrap=True)
    table.add_column("Description", ratio=1)

    for key, preset in THEMES.items():
        active = key == THEME.name
        name_cell = Text()
        if active:
            name_cell.append("● ", style=THEME.accent)
            name_cell.append(key, style=f"bold {THEME.brand}")
        else:
            name_cell.append("  ", style="")
            name_cell.append(key, style=THEME.secondary)

        swatch = Text()
        swatch.append("ρ ", style=f"bold {preset.mark}")
        swatch.append("● ", style=preset.code)
        swatch.append("● ", style=preset.success)
        swatch.append("● ", style=preset.warning)
        swatch.append("●", style=preset.error)

        desc = Text(preset.description, style=THEME.primary if active else THEME.muted)
        table.add_row(name_cell, swatch, desc)

    footer = Text(
        f"Switch with: {command_prefix} <name>   (saved to ~/.config/rho-agent/theme)",
        style=THEME.muted,
    )
    return Panel(
        Group(table, Text(""), footer),
        title=f"[{THEME.secondary}]Themes[/{THEME.secondary}]",
        title_align="left",
        box=box.SQUARE,
        border_style=THEME.border,
        padding=(0, 1),
    )


def render_access_card(session: Any) -> Panel:
    """Minimal security posture card for `/access`."""
    agent = session.agent
    config = agent.config
    level = config.access.level.value if config.backend == "local" else "remote"

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=THEME.muted, no_wrap=True)
    grid.add_column()
    grid.add_row("level", _access_label(level, backend=config.backend))
    grid.add_row("summary", Text(access_summary(agent), style=THEME.subtle))

    blocks: list[RenderableType] = [grid]
    sandbox = getattr(agent.registry, "sandbox", None)
    if sandbox is not None:
        policy = sandbox.with_grants(session.access_grants).policy
        blocks.append(Text(""))
        blocks.append(Text(policy.describe(), style=THEME.muted))

    if session.access_grants:
        blocks.append(Text(""))
        blocks.append(Text("Session grants", style=f"bold {THEME.primary}"))
        for grant in session.access_grants:
            kind = "write" if grant.write else "read"
            row = Text("  ")
            row.append(f"{kind:<6}", style=THEME.warning if grant.write else THEME.secondary)
            row.append(_shorten_path(grant.path), style=THEME.primary)
            blocks.append(row)
        blocks.append(Text("  Revoke with /access revoke <path>", style=THEME.muted))

    blocks.append(Text(""))
    blocks.append(Text("Switch: /access read-only | workspace | full", style=THEME.muted))

    return Panel(
        Group(*blocks),
        title=f"[{THEME.secondary}]Access & Sandbox[/{THEME.secondary}]",
        title_align="left",
        box=box.SQUARE,
        border_style=THEME.border,
        padding=(0, 1),
    )


def render_session_status_card(
    session: Any,
    token_status: TokenStatus,
    working_dir: str,
) -> Panel:
    """Minimal telemetry card for `/status`."""
    config = session.agent.config
    ratio = (
        min(token_status.context_size / token_status.context_window, 1.0)
        if token_status.context_window > 0 and token_status.context_size > 0
        else 0.0
    )
    filled = round(ratio * 16)
    ctx_bar = ("━" * filled) + ("─" * (16 - filled))
    bar_color = THEME.error if ratio >= 0.85 else (THEME.warning if ratio >= 0.65 else THEME.accent)

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=THEME.muted, no_wrap=True)
    grid.add_column()

    grid.add_row("session", Text(f"#{session.id}", style=THEME.primary))
    grid.add_row("model", Text(config.model or "", style=THEME.secondary))
    grid.add_row("project", Text(_shorten_path(working_dir), style=THEME.muted))
    grid.add_row("messages", Text(str(len(session.state.messages)), style=THEME.secondary))

    ctx_line = Text()
    ctx_line.append(ctx_bar[:filled], style=bar_color)
    ctx_line.append(ctx_bar[filled:], style=THEME.border)
    ctx_line.append(
        f"  {ratio:.1%}  ({_format_token_count(token_status.context_size)} / "
        f"{_format_token_count(token_status.context_window)})",
        style=THEME.subtle,
    )
    grid.add_row("context", ctx_line)

    tok_line = Text()
    tok_line.append(
        f"{_format_token_count(token_status.total_input_tokens)} in", style=THEME.primary
    )
    if token_status.total_cached_tokens:
        tok_line.append(f" ({token_status.cache_hit_rate:.0%} cached)", style=THEME.muted)
    tok_line.append("  ·  ", style=THEME.border_active)
    tok_line.append(
        f"{_format_token_count(token_status.total_output_tokens)} out", style=THEME.primary
    )
    if token_status.total_reasoning_tokens:
        tok_line.append(
            f" ({_format_token_count(token_status.total_reasoning_tokens)} think)",
            style=THEME.muted,
        )
    grid.add_row("tokens", tok_line)

    cost_str = _format_cost(token_status.total_cost_usd) or "$0.00"
    grid.add_row("cost", Text(cost_str, style=THEME.primary))

    return Panel(
        grid,
        title=f"[{THEME.secondary}]Status[/{THEME.secondary}]",
        title_align="left",
        box=box.SQUARE,
        border_style=THEME.border,
        padding=(0, 1),
    )


def render_saved_sessions_table(
    sessions: list[Any],
    *,
    numbered: bool = False,
) -> Table:
    """Clean borderless hairline table for `--list` and `/resume`."""
    table = Table(
        show_header=True,
        header_style=THEME.muted,
        box=box.SIMPLE_HEAD,
        border_style=THEME.border,
        expand=True,
        pad_edge=False,
    )
    if numbered:
        table.add_column("#", style=THEME.muted, justify="right", no_wrap=True)
    table.add_column("Session", style=THEME.primary, no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Model", style=THEME.muted, no_wrap=True)
    table.add_column("Active", justify="right", style=THEME.muted, no_wrap=True)
    table.add_column("Cost", justify="right", style=THEME.secondary, no_wrap=True)
    table.add_column("Preview", ratio=1, no_wrap=True, overflow="ellipsis")

    now = datetime.now(UTC)
    for idx, info in enumerate(sessions, start=1):
        glyph, color = status_style(info.status)
        try:
            then = datetime.fromisoformat(info.last_activity or info.created_at)
            days = (now - then).days
            age = f"{days}d" if days >= 2 else _format_elapsed(then, now)
        except (ValueError, TypeError):
            age = "?"

        row: list[Any] = []
        if numbered:
            row.append(str(idx))
        row.extend(
            [
                Text(info.id, style=THEME.primary),
                Text(f"{glyph} {info.status}", style=color),
                Text(info.model, style=THEME.muted),
                age,
                _format_cost(info.cost_usd),
                Text(info.display_preview, style=THEME.subtle),
            ]
        )
        table.add_row(*row)
    return table


class TurnRenderer:
    """Coordinates live parallel-safe tool rails, pulse indicator, and streaming Markdown.

    Visual hierarchy for a turn:
    - Agent work (tools) is indented and rendered atomically on completion so
      parallel tool calls never interleave outputs.
    - Agent responses are anchored cleanly by a subtle ``rho`` header/divider
      and full-brightness Markdown at column 0.
    """

    def __init__(
        self,
        *,
        console: Console = default_console,
        interactive_tty: bool = True,
        render_markdown: bool = True,
        working_dir: str | None = None,
        model: str = "",
        context_window: int | None = None,
        token_status: TokenStatus | None = None,
        show_turn_footer: bool = False,
    ) -> None:
        self._console = console
        self._interactive_tty = interactive_tty
        self._render_markdown = interactive_tty and render_markdown
        self._working_dir = working_dir
        self._model = model
        self._context_window = context_window
        self._token_status = token_status
        self._show_turn_footer = show_turn_footer

        self._turn_start = monotonic()
        self._phase_start = self._turn_start
        self._phase_label = "Thinking"
        self._frame_idx = 0

        self._spinner_live: Live | None = None
        self._ticker_task: asyncio.Task[None] | None = None

        self._md_live: Live | None = None
        self._text_chunks: list[str] = []
        self._last_md_refresh = 0.0

        # Active in-flight tools keyed by tool_call_id -> (name, args, start_time)
        self._active_tools: dict[str, tuple[str | None, dict[str, Any] | None, float]] = {}
        self._tool_seq = 0
        self._had_output = False
        self._in_text_stream = False
        self._in_tool_section = False
        self._showed_agent_anchor = False

    def start(self) -> None:
        """Start the live ticking status indicator."""
        self._turn_start = monotonic()
        self._set_spinner("Thinking")

    def stop(self) -> None:
        """Cleanly finalize any open live displays and stop background timers."""
        self._stop_spinner()
        self._active_tools.clear()
        self._flush_text_stream()
        self._flush_markdown(final=True)

    def _spinner_renderable(self) -> RenderableType:
        frame = _SPINNER_FRAMES[self._frame_idx % len(_SPINNER_FRAMES)]
        now = monotonic()
        if self._active_tools:
            lines: list[Text] = []
            for _key, (tname, targs, started) in self._active_tools.items():
                target, _ = _compact_arg_target(tname, targs, self._working_dir)
                short = target[:52] + "…" if len(target) > 52 else target
                row = Text()
                row.append(f"  {frame} ", style=THEME.accent)
                row.append(tname or "tool", style=THEME.secondary)
                if short:
                    row.append(f"  {short}", style=THEME.muted)
                row.append(f"  {_format_duration(now - started)}", style=THEME.border_active)
                lines.append(row)
            return Group(*lines)

        elapsed = _format_duration(now - self._phase_start)
        line = Text()
        line.append(f"  {frame} ", style=THEME.accent)
        line.append(f"{self._phase_label}", style=THEME.subtle)
        line.append(f"  {elapsed}", style=THEME.muted)
        line.append("  ·  Ctrl+C cancel", style=THEME.border_active)
        return line

    def _set_spinner(self, label: str) -> None:
        if not self._interactive_tty:
            return
        self._phase_label = label
        self._phase_start = monotonic()
        if self._spinner_live is None:
            self._spinner_live = Live(
                self._spinner_renderable(),
                console=self._console,
                auto_refresh=False,
                transient=True,
            )
            self._spinner_live.start(refresh=True)
            with suppress(RuntimeError):
                loop = asyncio.get_running_loop()
                self._ticker_task = loop.create_task(self._tick_loop())
        else:
            self._spinner_live.update(self._spinner_renderable(), refresh=True)

    async def _tick_loop(self) -> None:
        try:
            while self._spinner_live is not None:
                self._frame_idx = (self._frame_idx + 1) % len(_SPINNER_FRAMES)
                self._spinner_live.update(self._spinner_renderable(), refresh=True)
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            pass

    def _stop_spinner(self) -> None:
        if self._ticker_task is not None:
            self._ticker_task.cancel()
            self._ticker_task = None
        if self._spinner_live is not None:
            with suppress(Exception):
                self._spinner_live.stop()
            self._spinner_live = None

    def _ensure_response_header(self) -> None:
        """Print a subtle visual boundary before the agent's text response."""
        if self._in_tool_section:
            self._console.print()
            self._console.print(render_agent_divider(after_tools=True))
            self._console.print()
            self._in_tool_section = False
            self._showed_agent_anchor = True
        elif not self._showed_agent_anchor:
            self._console.print(render_agent_divider(after_tools=False))
            self._console.print()
            self._showed_agent_anchor = True

    def _flush_text_stream(self) -> None:
        """Terminate an in-progress raw (non-Markdown) text stream with a newline."""
        if self._in_text_stream:
            print(flush=True)
            self._in_text_stream = False

    def _update_markdown(self) -> None:
        combined = "".join(self._text_chunks)
        if not combined.strip():
            return
        now = monotonic()
        if self._md_live is None:
            self._ensure_response_header()
            self._had_output = True
            self._md_live = Live(
                make_markdown(combined),
                console=self._console,
                refresh_per_second=12,
                transient=False,
                vertical_overflow="visible",
            )
            self._md_live.start()
            self._last_md_refresh = now
        elif now - self._last_md_refresh >= 0.08:
            self._md_live.update(make_markdown(combined))
            self._last_md_refresh = now

    def _flush_markdown(self, *, final: bool = False) -> None:
        if not self._render_markdown:
            return
        combined = "".join(self._text_chunks).strip()
        if self._md_live is not None:
            if combined:
                self._md_live.update(make_markdown(combined))
            with suppress(Exception):
                self._md_live.stop()
            self._md_live = None
        elif combined:
            self._ensure_response_header()
            self._had_output = True
            try:
                self._console.print(make_markdown(combined))
            except Exception:
                print(combined, flush=True)
        self._text_chunks.clear()

    def pause_for_prompt(self) -> None:
        """Suspend live displays cleanly before interactive approval input."""
        self._stop_spinner()
        self._flush_text_stream()
        self._flush_markdown(final=True)

    def resume_after_prompt(self) -> None:
        """Resume live spinner cleanly after an approval prompt finishes."""
        now = monotonic()
        if self._active_tools:
            # Reset start timers so waiting on the user isn't billed as tool execution time
            for key, (tname, targs, _started) in list(self._active_tools.items()):
                self._active_tools[key] = (tname, targs, now)
            self._set_spinner("Running")
        else:
            self._set_spinner("Thinking")

    def _pop_tool(
        self, event: AgentEvent
    ) -> tuple[str | None, dict[str, Any] | None, float | None]:
        """Match a finishing tool event back to its recorded start entry."""
        key = event.tool_call_id or ""
        if key and key in self._active_tools:
            tname, targs, started = self._active_tools.pop(key)
            return tname or event.tool_name, targs, monotonic() - started
        # Fallback: match first active tool with the same name
        for k, (tname, targs, started) in list(self._active_tools.items()):
            if tname == event.tool_name:
                self._active_tools.pop(k)
                return tname, targs, monotonic() - started
        return event.tool_name, event.tool_args, None

    def on_event(self, event: AgentEvent) -> None:
        """Render an ``AgentEvent`` with clear user / work / response separation."""
        etype = event.type

        if etype == "text":
            chunk = event.content or ""
            if not chunk:
                return
            self._stop_spinner()
            if self._render_markdown:
                self._text_chunks.append(chunk)
                self._update_markdown()
            else:
                if not self._in_text_stream:
                    if self._interactive_tty:
                        self._ensure_response_header()
                    self._in_text_stream = True
                self._had_output = True
                print(chunk, end="", flush=True)
            return

        if etype in (
            "tool_start",
            "tool_end",
            "tool_blocked",
            "compact_start",
            "compact_end",
            "retry",
            "turn_complete",
            "error",
            "cancelled",
        ):
            self._flush_text_stream()
            self._flush_markdown(final=True)

        if etype == "tool_start":
            self._tool_seq += 1
            key = event.tool_call_id or f"{event.tool_name}:{self._tool_seq}"
            self._active_tools[key] = (event.tool_name, event.tool_args, monotonic())
            if self._interactive_tty:
                # Show all in-flight tools live; commit atomically on tool_end
                # so parallel tool calls never interleave outputs!
                if not self._in_tool_section and self._had_output:
                    self._stop_spinner()
                    self._console.print()
                self._in_tool_section = True
                self._set_spinner(f"Running {event.tool_name}")
            else:
                self._in_tool_section = True
                self._had_output = True
                self._console.print(
                    render_tool_start(
                        event.tool_name,
                        event.tool_args,
                        working_dir=self._working_dir,
                    )
                )

        elif etype == "tool_end":
            self._stop_spinner()
            tname, targs, elapsed = self._pop_tool(event)
            self._in_tool_section = True
            self._had_output = True
            if self._interactive_tty:
                self._console.print(
                    render_tool_block(
                        tname,
                        targs,
                        event.tool_metadata,
                        event.tool_result,
                        working_dir=self._working_dir,
                        elapsed=elapsed,
                    )
                )
                self._set_spinner("Thinking")
            else:
                block = render_tool_end(
                    tname,
                    event.tool_metadata,
                    event.tool_result,
                    elapsed=elapsed,
                )
                if block is not None:
                    self._console.print(block)

        elif etype == "tool_blocked":
            self._stop_spinner()
            tname, targs, elapsed = self._pop_tool(event)
            # Remaining sibling tool calls in the same batch are skipped by Session
            self._active_tools.clear()
            self._in_tool_section = True
            self._had_output = True
            if self._interactive_tty:
                self._console.print(
                    render_tool_block(
                        tname,
                        targs,
                        None,
                        None,
                        working_dir=self._working_dir,
                        elapsed=elapsed,
                        blocked=True,
                    )
                )
                self._set_spinner("Thinking")
            else:
                footer = Text()
                footer.append("  └ ", style=THEME.rail)
                footer.append("rejected by user", style=THEME.warning)
                self._console.print(footer)

        elif etype == "retry":
            self._stop_spinner()
            msg = event.content or "transient error"
            self._console.print(_markup(f"  retrying ({msg})", THEME.warning))
            self._set_spinner("Retrying")

        elif etype == "compact_start":
            self._stop_spinner()
            trigger = event.content or "manual"
            msg = (
                "Context approaching limit — auto-compacting…"
                if trigger == "auto"
                else "Compacting conversation…"
            )
            self._console.print(_markup(f"  {msg}", THEME.warning))
            self._set_spinner("Compacting")

        elif etype == "compact_end":
            self._stop_spinner()
            self._console.print(_markup(f"  {event.content or 'Compacted'}", THEME.secondary))
            self._console.print(
                _markup(
                    "  Tip: start a fresh session when switching tasks for best accuracy.",
                    THEME.muted,
                )
            )

        elif etype == "cancelled":
            self._stop_spinner()
            self._active_tools.clear()

        elif etype == "turn_complete":
            self._stop_spinner()
            self._active_tools.clear()
            if self._token_status:
                self._token_status.update(event.usage)
            if self._show_turn_footer:
                self._console.print()
                self._console.print(
                    render_turn_footer(
                        event.usage,
                        model=self._model,
                        elapsed=monotonic() - self._turn_start,
                        context_window=self._context_window,
                    )
                )

        elif etype == "error":
            self._stop_spinner()
            self._active_tools.clear()
            self._console.print(
                Panel(
                    _markup(str(event.content or "Unknown error"), THEME.error),
                    title=f"[{THEME.error}]Error[/{THEME.error}]",
                    title_align="left",
                    box=box.SQUARE,
                    border_style=THEME.error,
                    padding=(0, 1),
                )
            )
