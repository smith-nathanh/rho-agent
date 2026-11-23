"""Pure-Python live Mission Control dashboard and trace event renderer for ``rho-agent monitor``."""

from __future__ import annotations

import json
import os
import select
import signal
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

from rich import box
from rich.console import Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..core.approvals import ApprovalRequest, respond
from ..core.session_meta import LIVE_STATUSES, effective_status, read_meta
from ..core.session_store import SessionInfo, SessionStore
from .formatting import (
    _format_cost,
    _format_token_count,
    _format_tool_signature,
    _is_interactive_terminal,
    _shorten_path,
)
from .session_table import _ago, _project_label, build_fleet_summary, fleet_counts
from .state import console
from .theme import THEME, status_style, tool_visual
from .ui import _build_edit_diff, _compact_arg_target

_TAIL_CHUNK_BYTES = 64 * 1024
# Cache of (path, limit) -> (mtime_ns, size, lines)
_TRACE_TAIL_CACHE: dict[tuple[Path, int], tuple[int, int, list[str]]] = {}


def write_directive(session_dir: Path, text: str) -> tuple[bool, str, str]:
    """Append a directive for ``session_dir``.

    Returns ``(ok, detail_message, effective_session_status)``.
    """
    meta = read_meta(session_dir)
    status = effective_status(meta) if meta else "running"
    if status not in LIVE_STATUSES and meta.get("parent_session_id"):
        return False, f"{session_dir.name} has finished ({status}); nothing to steer", status

    path = session_dir / "directives.jsonl"
    entry = json.dumps({"text": text}) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(entry)
            finally:
                if fcntl is not None:
                    fcntl.flock(f, fcntl.LOCK_UN)
        return True, f"directive queued for {session_dir.name}", status
    except OSError as exc:
        return False, f"Failed to write directive: {exc}", status


def _event_timestamp(event: dict[str, Any]) -> str:
    raw = event.get("timestamp") or event.get("ts") or ""
    if not raw or not isinstance(raw, str):
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M:%S")
    except ValueError:
        return ""


def render_trace_event(
    line: str,
    *,
    compact: bool = False,
    working_dir: str | None = None,
) -> RenderableType | None:
    """Format a single ``trace.jsonl`` line into a clean architectural timeline row."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None

    ev_type = event.get("type", "")
    ts = _event_timestamp(event)
    ts_prefix = f"{ts} " if ts else ""

    def prefix(rail: str = "│ ", rail_color: str = THEME.rail) -> Text:
        t = Text()
        if ts_prefix:
            t.append(ts_prefix, style=THEME.muted)
        t.append(rail, style=rail_color)
        return t

    if ev_type == "run_start":
        prompt = " ".join(str(event.get("prompt", "")).split())
        cap = 72 if compact else 110
        if len(prompt) > cap:
            prompt = prompt[: cap - 1] + "…"
        row = prefix("┌ ", THEME.border_active)
        row.append("run  ", style=f"bold {THEME.primary}")
        row.append(prompt, style=THEME.secondary)
        return row

    if ev_type == "run_end":
        status = str(event.get("status", "?"))
        glyph, color = status_style(status)
        row = prefix("└ ", THEME.border_active)
        row.append(f"{glyph} {status}", style=color)
        return row

    if ev_type == "llm_start":
        if compact:
            return None
        model = event.get("model", "?")
        ctx = event.get("context_size", "?")
        row = prefix()
        row.append(f"llm  {model}  ctx={ctx}", style=THEME.muted)
        return row

    if ev_type == "llm_end":
        inp = int(event.get("input_tokens", 0) or 0)
        out = int(event.get("output_tokens", 0) or 0)
        cost = float(event.get("cost_usd", 0.0) or 0.0)
        row = prefix()
        row.append(
            f"tokens  {_format_token_count(inp)} in / {_format_token_count(out)} out",
            style=THEME.muted,
        )
        cost_str = _format_cost(cost)
        if cost_str:
            row.append(f"  {cost_str}", style=THEME.subtle)
        return row

    if ev_type == "tool_start":
        name = event.get("tool_name", "?")
        args = event.get("tool_args") if isinstance(event.get("tool_args"), dict) else {}
        _, color = tool_visual(name)
        target, detail = _compact_arg_target(name, args, working_dir)
        cap = 54 if compact else 110
        if len(target) > cap:
            target = target[: cap - 1] + "…"
        row = prefix()
        row.append(name, style=f"bold {color}")
        if target:
            row.append(f"  {target}", style=THEME.tool_call)
        if detail and not compact:
            row.append(f"  {detail}", style=THEME.muted)

        if not compact and name == "edit":
            old_s = args.get("old_string")
            new_s = args.get("new_string")
            if isinstance(old_s, str) and isinstance(new_s, str) and (old_s or new_s):
                diff_rows, added, removed = _build_edit_diff(old_s, new_s)
                if added or removed:
                    row.append("  ", style="")
                    if added:
                        row.append(f"+{added}", style=THEME.diff_add)
                    if added and removed:
                        row.append(" ", style="")
                    if removed:
                        row.append(f"-{removed}", style=THEME.diff_del)
                if diff_rows:
                    pad = " " * len(ts_prefix)
                    aligned: list[Text] = []
                    for drow in diff_rows[:8]:
                        t = Text(pad)
                        t.append_text(drow)
                        aligned.append(t)
                    return Group(row, *aligned)
        return row

    if ev_type == "tool_end":
        name = event.get("tool_name", "?")
        ok = event.get("success", True)
        row = prefix()
        row.append("└ ", style=THEME.rail)
        if ok:
            row.append(f"{name} ok", style=THEME.muted)
        else:
            row.append(f"× {name} failed", style=THEME.error)
        return row

    if ev_type == "tool_blocked":
        name = event.get("tool_name", "?")
        row = prefix()
        row.append("└ ", style=THEME.rail)
        row.append(f"{name} blocked", style=THEME.warning)
        return row

    if ev_type == "message":
        role = event.get("role", "?")
        content = event.get("content", "")
        if role == "assistant" and isinstance(content, str) and content.strip():
            clean = " ".join(content.split())
            cap = 78 if compact else 180
            if len(clean) > cap:
                clean = clean[: cap - 1] + "…"
            row = prefix()
            row.append(clean, style=THEME.primary)
            return row
        return None

    if ev_type == "approval_requested":
        signature = _format_tool_signature(event.get("tool_name"), event.get("tool_args"))
        req_id = event.get("request_id", "?")
        row = prefix("│ ", THEME.warning)
        row.append(f"approval {req_id}  ", style=f"bold {THEME.primary}")
        row.append(signature, style=THEME.secondary)
        return row

    if ev_type == "approval_resolved":
        outcome = str(event.get("outcome", "?"))
        req_id = event.get("request_id", "?")
        color = THEME.secondary if outcome == "approved" else THEME.warning
        row = prefix("│ ", THEME.border_active)
        row.append(f"approval {req_id} {outcome}", style=color)
        return row

    if ev_type == "child_start":
        child = event.get("child_session_id", "?")
        tool = event.get("tool_name", "?")
        instruction = " ".join(str(event.get("instruction", "")).split())
        cap = 50 if compact else 96
        if len(instruction) > cap:
            instruction = instruction[: cap - 1] + "…"
        row = prefix("├─ ", THEME.border_active)
        row.append("spawn ", style=THEME.accent)
        row.append(f"{child} ({tool})  ", style=THEME.primary)
        row.append(instruction, style=THEME.muted)
        return row

    if ev_type == "child_end":
        child = event.get("child_session_id", "?")
        status = str(event.get("status", "?"))
        row = prefix("└─ ", THEME.border)
        row.append(f"child {child} {status}", style=THEME.muted)
        return row

    if ev_type == "compact":
        before = event.get("tokens_before", "?")
        after = event.get("tokens_after", "?")
        row = prefix()
        row.append(f"compact  {before} → {after} tokens", style=THEME.muted)
        return row

    return None


def _flatten_fleet(
    sessions: list[SessionInfo],
    *,
    live_only: bool,
) -> list[tuple[SessionInfo, list[bool]]]:
    """Flatten sessions grouped by project into ``(info, tree_branches)`` rows."""

    def has_live(info: SessionInfo) -> bool:
        return info.live or any(has_live(c) for c in info.children)

    filtered = [s for s in sessions if not live_only or has_live(s)]

    by_project: dict[str, list[SessionInfo]] = {}
    for info in filtered:
        by_project.setdefault(info.cwd, []).append(info)

    rows: list[tuple[SessionInfo, list[bool]]] = []

    def add(info: SessionInfo, branches: list[bool]) -> None:
        rows.append((info, branches))
        n = len(info.children)
        for idx, child in enumerate(info.children):
            add(child, [*branches, idx < n - 1])

    for group in by_project.values():
        for info in group:
            add(info, [])
    return rows


def _tail_trace_lines(session_dir: Path | None, limit: int = 24) -> list[str]:
    """Read the last ``limit`` non-empty lines of ``trace.jsonl`` via bounded EOF seek + cache."""
    if session_dir is None:
        return []
    trace_path = session_dir / "trace.jsonl"
    try:
        st = trace_path.stat()
    except OSError:
        return []

    cache_key = (trace_path, limit)
    cached = _TRACE_TAIL_CACHE.get(cache_key)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]

    try:
        with open(trace_path, "rb") as f:
            size = st.st_size
            if size <= _TAIL_CHUNK_BYTES:
                data = f.read()
                lines = [
                    ln for ln in data.decode("utf-8", errors="replace").splitlines() if ln.strip()
                ]
            else:
                f.seek(size - _TAIL_CHUNK_BYTES)
                data = f.read(_TAIL_CHUNK_BYTES)
                text = data.decode("utf-8", errors="replace")
                # Drop potentially partial first line from mid-file seek
                first_nl = text.find("\n")
                if first_nl != -1:
                    text = text[first_nl + 1 :]
                lines = [ln for ln in text.splitlines() if ln.strip()]
        result = lines[-limit:]
        if len(_TRACE_TAIL_CACHE) > 64:
            _TRACE_TAIL_CACHE.clear()
        _TRACE_TAIL_CACHE[cache_key] = (st.st_mtime_ns, st.st_size, result)
        return result
    except OSError:
        return []


def _read_key_nonblocking(timeout: float, *, fd: int | None = None) -> str | None:
    """Read a single keypress within ``timeout`` seconds, draining full escape sequences.

    Properly drains multi-byte CSI/SS3 sequences (e.g. Shift+Arrow ``\\x1b[1;2A``,
    PgUp ``\\x1b[5~``, F-keys) so trailing bytes never leak as hotkeys.
    """
    if fd is None:
        fd = sys.stdin.fileno()
    ready, _, _ = select.select([fd], [], [], timeout)
    if not ready:
        return None
    raw = os.read(fd, 1)
    if not raw:
        return "EOF"
    ch = raw.decode("utf-8", errors="ignore")
    if ch == "\x1b":
        more, _, _ = select.select([fd], [], [], 0.03)
        if not more:
            return "ESC"
        second = os.read(fd, 1)
        if not second:
            return "ESC"
        seq = bytearray(second)
        if second == b"[":
            # CSI sequence: read until final byte in 0x40..0x7E (or max 16 bytes)
            for _ in range(15):
                nxt, _, _ = select.select([fd], [], [], 0.02)
                if not nxt:
                    break
                b = os.read(fd, 1)
                if not b:
                    break
                seq.extend(b)
                if 0x40 <= b[0] <= 0x7E:
                    break
            decoded = seq.decode("utf-8", errors="ignore")
            if decoded == "[A":
                return "UP"
            if decoded == "[B":
                return "DOWN"
            return "ESC_SEQ"
        if second == b"O":
            # SS3 sequence (application cursor mode arrows, F1-F4)
            nxt, _, _ = select.select([fd], [], [], 0.02)
            if nxt:
                seq.extend(os.read(fd, 1))
            decoded = seq.decode("utf-8", errors="ignore")
            if decoded == "OA":
                return "UP"
            if decoded == "OB":
                return "DOWN"
            return "ESC_SEQ"
        return "ESC_SEQ"
    return ch


class MissionControl:
    """Minimal, architectural live Mission Control dashboard."""

    def __init__(self, store: SessionStore) -> None:
        self._store = store
        self._selected_id: str | None = None
        self._live_only = False
        self._toast: tuple[str, str, float] | None = None

    def _set_toast(self, msg: str, color: str = THEME.secondary, ttl: float = 4.0) -> None:
        self._toast = (msg, color, monotonic() + ttl)

    def _active_toast(self) -> tuple[str, str] | None:
        if self._toast is None:
            return None
        msg, color, expires = self._toast
        if monotonic() > expires:
            self._toast = None
            return None
        return msg, color

    def _snapshot(
        self,
    ) -> tuple[
        list[SessionInfo],
        list[tuple[SessionInfo, list[bool]]],
        list[tuple[SessionInfo, ApprovalRequest]],
        SessionInfo | None,
    ]:
        sessions = self._store.list(limit=50)
        pending = self._store.pending_approvals()
        flat = _flatten_fleet(sessions, live_only=self._live_only)

        selected: SessionInfo | None = None
        if flat:
            if self._selected_id:
                for info, _ in flat:
                    if info.id == self._selected_id:
                        selected = info
                        break
            if selected is None:
                selected = flat[0][0]
                self._selected_id = selected.id
        else:
            self._selected_id = None
        return sessions, flat, pending, selected

    def _move_selection(self, delta: int) -> None:
        _, flat, _, _ = self._snapshot()
        if not flat:
            return
        ids = [info.id for info, _ in flat]
        try:
            idx = ids.index(self._selected_id) if self._selected_id in ids else 0
        except ValueError:
            idx = 0
        self._selected_id = ids[(idx + delta) % len(ids)]

    def build_layout(self) -> Layout:
        """Render the clean 1px square split-pane Mission Control layout."""
        sessions, flat, pending, selected = self._snapshot()
        pending_by_session: dict[str, list[ApprovalRequest]] = {}
        for info, req in pending:
            pending_by_session.setdefault(info.id, []).append(req)

        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=2),
        )
        layout["body"].split_row(
            Layout(name="fleet", ratio=11),
            Layout(name="inspector", ratio=10),
        )

        # --- Header ---
        width = console.width
        if width < 120:
            counts, total_cost, _ = fleet_counts(sessions)
            summary = Text()
            summary.append("ρ ", style=f"bold {THEME.mark}")
            summary.append("fleet ", style=f"bold {THEME.brand}")
            if counts["running"]:
                summary.append(f"●{counts['running']} ", style=THEME.accent)
            if pending:
                summary.append(f"●{len(pending)} ", style=THEME.warning)
            if counts["waiting_input"]:
                summary.append(f"○{counts['waiting_input']} ", style=THEME.secondary)
            if counts["paused"]:
                summary.append(f"▪{counts['paused']} ", style=THEME.muted)
            cost_str = _format_cost(total_cost)
            if cost_str:
                summary.append(f"· {cost_str}", style=THEME.subtle)
        else:
            summary = build_fleet_summary(sessions, pending_approvals=len(pending))

        right = Text()
        mode_tag = "live" if self._live_only else "all"
        right.append(mode_tag, style=THEME.primary if self._live_only else THEME.muted)
        right.append("  ·  ", style=THEME.border)
        right.append(datetime.now().strftime("%H:%M:%S"), style=THEME.muted)

        header_grid = Table.grid(expand=True)
        header_grid.add_column(ratio=1)
        header_grid.add_column(justify="right", no_wrap=True)
        header_grid.add_row(summary, right)
        layout["header"].update(
            Panel(
                header_grid,
                box=box.SQUARE,
                border_style=THEME.border,
                padding=(0, 1),
            )
        )

        layout["fleet"].update(self._build_fleet_panel(flat, pending_by_session, selected))
        layout["inspector"].update(
            self._build_inspector_panel(selected, pending, pending_by_session)
        )
        layout["footer"].update(self._build_footer())
        return layout

    def _build_fleet_panel(
        self,
        flat: list[tuple[SessionInfo, list[bool]]],
        pending_by_session: dict[str, list[ApprovalRequest]],
        selected: SessionInfo | None,
    ) -> Panel:
        if not flat:
            empty = Text(
                "No sessions match the current filter.\nPress f to toggle live/all.",
                style=THEME.muted,
            )
            return Panel(
                empty,
                title=f"[{THEME.secondary}]Fleet[/{THEME.secondary}]",
                title_align="left",
                box=box.SQUARE,
                border_style=THEME.border,
                padding=(1, 2),
            )

        width = console.width
        show_cost = width >= 94
        show_active = width >= 116
        show_task = width >= 134

        table = Table(
            show_header=True,
            header_style=THEME.muted,
            box=None,
            expand=True,
            pad_edge=False,
        )
        table.add_column("", width=2, no_wrap=True)
        table.add_column("Session", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        if show_active:
            table.add_column("Active", justify="right", style=THEME.muted, no_wrap=True)
        if show_cost:
            table.add_column("Cost", justify="right", style=THEME.secondary, no_wrap=True)
        if show_task:
            table.add_column("Task", ratio=1, no_wrap=True, overflow="ellipsis")

        now = datetime.now(UTC)
        extra_cols = int(show_active) + int(show_cost) + int(show_task)

        # Build all display rows first so we can apply clean viewport scrolling
        rendered_rows: list[tuple[list[Any], str, bool]] = []
        last_cwd: str | None = None
        selected_row_idx = 0

        for info, branches in flat:
            if not branches and info.cwd != last_cwd:
                last_cwd = info.cwd
                rendered_rows.append(
                    (
                        [
                            "",
                            Text(_project_label(info.cwd), style=f"bold {THEME.secondary}"),
                            "",
                            *([""] * extra_cols),
                        ],
                        "",
                        False,
                    )
                )

            is_sel = selected is not None and info.id == selected.id
            if is_sel:
                selected_row_idx = len(rendered_rows)
            cursor = Text("›", style=f"bold {THEME.accent}") if is_sel else Text(" ")

            id_cell = Text()
            if branches:
                for has_more in branches[:-1]:
                    id_cell.append("│ " if has_more else "  ", style=THEME.border)
                id_cell.append("├ " if branches[-1] else "└ ", style=THEME.border)
                id_cell.append(
                    info.id,
                    style=f"bold {THEME.brand}" if is_sel else THEME.secondary,
                )
            else:
                id_cell.append(
                    info.id,
                    style=f"bold {THEME.brand}" if is_sel else THEME.primary,
                )

            glyph, color = status_style(info.status)
            reqs = pending_by_session.get(info.id, [])
            st_cell = Text()
            st_cell.append(f"{glyph} ", style=color)
            short_status = {
                "waiting_approval": "approve",
                "waiting_input": "idle",
                "completed": "done",
            }.get(info.status, info.status)
            st_cell.append(
                short_status,
                style=THEME.primary if (reqs or is_sel) else THEME.subtle,
            )
            if reqs:
                st_cell.append(f" ({len(reqs)})", style=THEME.secondary)

            row_cells: list[Any] = [cursor, id_cell, st_cell]
            if show_active:
                row_cells.append(_ago(info.last_activity, now))
            if show_cost:
                row_cells.append(_format_cost(info.cost_usd))
            if show_task:
                row_cells.append(
                    Text(
                        info.display_preview or info.model,
                        style=THEME.primary if is_sel else THEME.muted,
                    )
                )

            row_style = f"on {THEME.selection_bg}" if is_sel else ""
            rendered_rows.append((row_cells, row_style, is_sel))

        # Viewport slicing around selected row when fleet exceeds terminal height
        # Chrome: header(3) + footer(2) + panel borders(2) + table header(1) = 8 rows
        max_visible = max(console.height - 9, 5)
        total_rows = len(rendered_rows)
        start = 0
        end = total_rows
        if total_rows > max_visible:
            half = max_visible // 2
            start = max(0, min(selected_row_idx - half, total_rows - max_visible))
            end = start + max_visible

        scroll_tag = ""
        if start > 0 or end < total_rows:
            scroll_tag = f" [{THEME.muted}]{start + 1}–{end}/{total_rows}[/{THEME.muted}]"

        for row_cells, row_style, _ in rendered_rows[start:end]:
            table.add_row(*row_cells, style=row_style)

        return Panel(
            table,
            title=(
                f"[{THEME.primary}]Fleet[/{THEME.primary}] "
                f"[{THEME.muted}]({len(flat)})[/{THEME.muted}]{scroll_tag}"
            ),
            title_align="left",
            box=box.SQUARE,
            border_style=THEME.border_active,
            padding=(0, 1),
        )

    def _build_inspector_panel(
        self,
        selected: SessionInfo | None,
        all_pending: list[tuple[SessionInfo, ApprovalRequest]],
        pending_by_session: dict[str, list[ApprovalRequest]],
    ) -> Panel:
        if selected is None:
            return Panel(
                Text("Select a session to inspect live activity.", style=THEME.muted),
                title=f"[{THEME.secondary}]Inspector[/{THEME.secondary}]",
                title_align="left",
                box=box.SQUARE,
                border_style=THEME.border,
                padding=(1, 2),
            )

        blocks: list[RenderableType] = []

        glyph, color = status_style(selected.status)
        meta_grid = Table.grid(padding=(0, 2), expand=True)
        meta_grid.add_column(style=THEME.muted, no_wrap=True)
        meta_grid.add_column(ratio=1)

        st_text = Text()
        st_text.append(f"{glyph} ", style=color)
        st_text.append(selected.status, style=THEME.primary)
        if selected.pid:
            st_text.append(f"  ·  pid {selected.pid}", style=THEME.muted)
        if selected.host:
            st_text.append(f"@{selected.host}", style=THEME.muted)
        meta_grid.add_row("status", st_text)

        model_text = Text(selected.model or "?", style=THEME.secondary)
        if selected.access:
            model_text.append(f"  ·  {selected.access}", style=THEME.muted)
        cost_str = _format_cost(selected.cost_usd)
        if cost_str:
            model_text.append(f"  ·  {cost_str}", style=THEME.primary)
        meta_grid.add_row("agent", model_text)

        if selected.cwd:
            meta_grid.add_row("project", Text(_project_label(selected.cwd), style=THEME.muted))
        blocks.append(meta_grid)

        sel_reqs = pending_by_session.get(selected.id, [])
        if sel_reqs:
            req = sel_reqs[0]
            sig = _format_tool_signature(req.tool_name, req.tool_args)
            req_body = Text()
            req_body.append(sig, style=THEME.primary)
            req_body.append("\n")
            req_body.append("a", style=f"bold {THEME.primary}")
            req_body.append(" approve   ", style=THEME.muted)
            req_body.append("A", style=f"bold {THEME.primary}")
            req_body.append(" all   ", style=THEME.muted)
            req_body.append("x", style=f"bold {THEME.primary}")
            req_body.append(" deny", style=THEME.muted)
            if len(sel_reqs) > 1:
                req_body.append(f"   (+{len(sel_reqs) - 1} more)", style=THEME.muted)
            blocks.append(Text(""))
            blocks.append(
                Panel(
                    req_body,
                    title=(
                        f"[{THEME.warning}]●[/{THEME.warning}] "
                        f"[bold {THEME.brand}]Approval · {escape(req.id)}[/bold {THEME.brand}]"
                    ),
                    title_align="left",
                    box=box.SQUARE,
                    border_style=THEME.border_active,
                    padding=(0, 1),
                )
            )
        elif all_pending:
            other_info, other_req = all_pending[0]
            hint = Text()
            hint.append("● ", style=THEME.warning)
            hint.append(
                f"{len(all_pending)} approval(s) pending on #{other_info.id} "
                f"({other_req.tool_name}) — press ",
                style=THEME.primary,
            )
            hint.append("a", style=f"bold {THEME.brand}")
            hint.append(" to approve", style=THEME.secondary)
            blocks.append(Text(""))
            blocks.append(hint)

        blocks.append(Text(""))
        blocks.append(Text("Trace", style=THEME.muted))
        raw_lines = _tail_trace_lines(selected.path, limit=28)
        rendered_events: list[RenderableType] = []
        for raw in raw_lines:
            item = render_trace_event(raw, compact=True, working_dir=selected.cwd or None)
            if item is not None:
                rendered_events.append(item)
        if rendered_events:
            blocks.extend(rendered_events[-16:])
        else:
            blocks.append(Text("  (no trace events yet)", style=THEME.muted))

        return Panel(
            Group(*blocks),
            title=(
                f"[{THEME.primary}]Inspector[/{THEME.primary}] "
                f"[{THEME.muted}]#{escape(selected.id)}[/{THEME.muted}]"
            ),
            title_align="left",
            box=box.SQUARE,
            border_style=THEME.border,
            padding=(0, 1),
        )

    def _build_footer(self) -> Group:
        toast = self._active_toast()
        toast_line = Text()
        if toast:
            msg, color = toast
            toast_line.append(f"  {msg}", style=color)
        else:
            toast_line.append(
                f"  {_shorten_path(str(self._store.base_dir))}",
                style=THEME.muted,
            )

        width = console.width
        if width < 82:
            keys = [
                ("↑↓", "nav"),
                ("a", "ok"),
                ("x", "deny"),
                ("p", "pause"),
                ("c", "stop"),
                ("d", "dir"),
                (":", "sh"),
                ("q", "quit"),
            ]
            sep = " "
        elif width < 112:
            keys = [
                ("↑↓", "nav"),
                ("a", "ok"),
                ("x", "deny"),
                ("p", "pause"),
                ("c", "cancel"),
                ("d", "dir"),
                ("w", "watch"),
                ("f", "live"),
                (":", "shell"),
                ("q", "quit"),
            ]
            sep = "  "
        else:
            keys = [
                ("↑↓", "select"),
                ("a", "approve"),
                ("A", "all"),
                ("x", "deny"),
                ("p", "pause"),
                ("d", "directive"),
                ("c", "cancel"),
                ("w", "watch"),
                ("f", "live/all"),
                (":", "shell"),
                ("q", "quit"),
            ]
            sep = "   "

        bar = Text("  ")
        for idx, (k, label) in enumerate(keys):
            if idx:
                bar.append(sep, style="")
            bar.append(k, style=THEME.primary)
            bar.append(f" {label}", style=THEME.muted)
        return Group(toast_line, bar)

    def _answer_selected_or_first(self, *, approved: bool, all_pending: bool = False) -> None:
        _, _, pending, selected = self._snapshot()
        if not pending:
            self._set_toast("No pending approvals.", THEME.muted)
            return
        if all_pending and approved:
            for _, req in pending:
                respond(req, True)
            self._set_toast(f"Approved all {len(pending)} pending request(s).", THEME.secondary)
            return

        target = next((pair for pair in pending if selected and pair[0].id == selected.id), None)
        if target is None:
            target = pending[0]
        info, req = target
        respond(req, approved)
        if approved:
            self._set_toast(f"Approved {req.id} ({req.tool_name} on #{info.id})", THEME.secondary)
        else:
            self._set_toast(f"Denied {req.id} ({req.tool_name} on #{info.id})", THEME.warning)

    def _toggle_pause(self) -> None:
        _, _, _, selected = self._snapshot()
        if selected is None or selected.path is None:
            return
        pause_file = selected.path / "pause"
        if pause_file.exists():
            pause_file.unlink(missing_ok=True)
            self._set_toast(f"Resumed #{selected.id}", THEME.secondary)
        elif selected.live:
            pause_file.touch()
            self._set_toast(f"Paused #{selected.id}", THEME.warning)
        else:
            self._set_toast(f"#{selected.id} is not running ({selected.status})", THEME.muted)

    def _cancel_selected(self) -> None:
        _, _, _, selected = self._snapshot()
        if selected is None:
            return
        if self._store.request_cancel(selected):
            self._set_toast(f"Cancel sent to #{selected.id}", THEME.warning)
        else:
            self._set_toast(f"#{selected.id} is not running ({selected.status})", THEME.muted)

    def _write_directive(self, session: SessionInfo, text: str) -> None:
        if session.path is None:
            return
        ok, msg, status = write_directive(session.path, text)
        if not ok:
            self._set_toast(msg, THEME.error)
        elif status not in LIVE_STATUSES:
            self._set_toast(
                f"Directive queued for #{session.id} (will read on resume)", THEME.warning
            )
        else:
            self._set_toast(f"Directive sent to #{session.id}", THEME.secondary)

    def run(self, *, on_watch: Any = None) -> str:
        """Run the interactive Mission Control dashboard."""
        if not _is_interactive_terminal():
            console.print(self.build_layout())
            return "quit"

        try:
            import termios
            import tty
        except ImportError:
            console.print(self.build_layout())
            return "quit"

        fd = sys.stdin.fileno()
        try:
            old_attrs = termios.tcgetattr(fd)
        except termios.error:
            console.print(self.build_layout())
            return "quit"

        exit_mode = "quit"
        prev_sigterm = None

        def _handle_sigterm(_signum: int, _frame: Any) -> None:
            with suppress(Exception):
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            raise SystemExit(143)

        with suppress(ValueError, OSError):
            prev_sigterm = signal.signal(signal.SIGTERM, _handle_sigterm)

        try:
            tty.setcbreak(fd)
            with Live(
                self.build_layout(),
                console=console,
                screen=True,
                auto_refresh=False,
            ) as live:
                try:
                    while True:
                        live.update(self.build_layout(), refresh=True)
                        key = _read_key_nonblocking(0.4, fd=fd)
                        if key is None or key == "ESC_SEQ":
                            continue
                        if key in ("q", "Q", "\x03", "\x04", "EOF"):
                            exit_mode = "quit"
                            break
                        if key == ":":
                            exit_mode = "shell"
                            break
                        if key in ("DOWN", "j"):
                            self._move_selection(1)
                        elif key in ("UP", "k"):
                            self._move_selection(-1)
                        elif key == "f":
                            self._live_only = not self._live_only
                            label = (
                                "Filter: live only" if self._live_only else "Filter: all sessions"
                            )
                            self._set_toast(label, THEME.secondary)
                        elif key == "a":
                            self._answer_selected_or_first(approved=True)
                        elif key == "A":
                            self._answer_selected_or_first(approved=True, all_pending=True)
                        elif key == "x":
                            self._answer_selected_or_first(approved=False)
                        elif key == "p":
                            self._toggle_pause()
                        elif key == "c":
                            self._cancel_selected()
                        elif key == "d":
                            _, _, _, selected = self._snapshot()
                            if selected is not None:
                                live.stop()
                                termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
                                try:
                                    sid = escape(selected.id)
                                    console.print(
                                        f"\n[{THEME.primary}]Directive for #{sid}[/{THEME.primary}] "
                                        f"[{THEME.muted}](blank to cancel):[/{THEME.muted}] ",
                                        end="",
                                    )
                                    text = input().strip()
                                    if text:
                                        self._write_directive(selected, text)
                                except (EOFError, KeyboardInterrupt):
                                    pass
                                finally:
                                    tty.setcbreak(fd)
                                    live.start()
                        elif key == "w" and on_watch is not None:
                            _, _, _, selected = self._snapshot()
                            if selected is not None:
                                live.stop()
                                termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
                                try:
                                    on_watch(selected.id, "20")
                                    console.print(
                                        f"\n[{THEME.muted}]Press Enter to return…[/{THEME.muted}]",
                                        end="",
                                    )
                                    with suppress(EOFError, KeyboardInterrupt):
                                        input()
                                finally:
                                    tty.setcbreak(fd)
                                    live.start()
                except KeyboardInterrupt:
                    exit_mode = "quit"
        finally:
            with suppress(Exception):
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            if prev_sigterm is not None:
                with suppress(ValueError, OSError):
                    signal.signal(signal.SIGTERM, prev_sigterm)

        return exit_mode
