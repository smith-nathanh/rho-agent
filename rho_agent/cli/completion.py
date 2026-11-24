"""Input completion: path, slash-command, and monitor completers."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from prompt_toolkit.completion import (
    Completer,
    Completion,
    merge_completers,
)
from prompt_toolkit.document import Document

from .theme import THEMES

# Slash commands with descriptions shown in the completion menu
COMMAND_SPECS: list[tuple[str, str]] = [
    ("/status", "Session tokens, context window & cost"),
    ("/model", "Compare prices and switch models"),
    ("/access", "Show or switch sandbox access level"),
    ("/access read-only", "Switch to read-only sandbox"),
    ("/access workspace", "Switch to workspace sandbox"),
    ("/access full", "Switch to full access (no sandbox)"),
    ("/access revoke", "Revoke an extra directory grant"),
    ("/compact", "Compact conversation history"),
    ("/resume", "List or switch to a saved session"),
    ("/resume latest", "Resume the most recent session"),
    ("/download", "Download file from remote sandbox"),
    ("/theme", "List or switch CLI color themes"),
    *[(f"/theme {name}", preset.description) for name, preset in THEMES.items()],
    ("/clear", "Clear the terminal screen"),
    ("/help", "Show commands and keybindings"),
    ("/exit", "Save and quit"),
    ("exit", "Save and quit"),
    ("quit", "Save and quit"),
]

COMMANDS = [cmd for cmd, _ in COMMAND_SPECS]

# Monitor shell commands with descriptions
MONITOR_COMMANDS: list[tuple[str, str]] = [
    ("ps", "List sessions grouped by project"),
    ("live", "Open live auto-refreshing Mission Control dashboard"),
    ("watch", "Follow a session trace live (watch <id> [n|all])"),
    ("inspect", "Show detailed metadata & recent events for a session"),
    ("approvals", "List pending tool approval requests"),
    ("review", "Step through pending approvals interactively"),
    ("approve", "Approve a pending request (approve <id|all>)"),
    ("deny", "Deny a pending request (deny <id>)"),
    ("directive", "Steer an agent mid-run (directive <id> <text>)"),
    ("pause", "Pause live session(s) before next LLM call"),
    ("resume", "Resume paused session(s)"),
    ("cancel", "Cancel live session(s) (cancel <id|all>)"),
    ("theme", "List or switch color theme (theme <name>)"),
    ("clear", "Clear the screen"),
    ("help", "Show monitor command reference"),
    ("exit", "Exit monitor"),
    ("quit", "Exit monitor"),
]

_SESSION_TARGET_CMDS = {"watch", "inspect", "pause", "resume", "cancel", "directive"}
_APPROVAL_TARGET_CMDS = {"approve", "deny"}

# Pattern to detect path-like strings in text
PATH_PATTERN = re.compile(
    r"(~/?|\.{1,2}/|/)?([a-zA-Z0-9_\-./]+/[a-zA-Z0-9_\-.]*|~[a-zA-Z0-9_\-./]*)$"
)


class SlashCommandCompleter(Completer):
    """Completes interactive slash commands with inline descriptions."""

    def get_completions(self, document: Document, complete_event: Any) -> Iterable[Completion]:
        text = document.text_before_cursor.lstrip()
        if not text:
            return
        lower = text.lower()
        for cmd, desc in COMMAND_SPECS:
            if cmd.startswith(lower):
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=cmd,
                    display_meta=desc,
                )


class InlinePathCompleter(Completer):
    """Completes file paths that appear anywhere in the input text."""

    def __init__(self, working_dir: str | None = None) -> None:
        self.working_dir = Path(working_dir).expanduser() if working_dir else Path.cwd()

    def get_completions(self, document: Document, complete_event: Any) -> Iterable[Completion]:
        text_before_cursor = document.text_before_cursor

        match = PATH_PATTERN.search(text_before_cursor)
        if not match:
            return

        path_text = match.group(0)
        start_pos = -len(path_text)

        # Expand paths for lookup
        if path_text.startswith("~"):
            expanded = os.path.expanduser(path_text)
        elif path_text.startswith("/"):
            expanded = path_text
        else:
            expanded = str(self.working_dir / path_text)

        path = Path(expanded)
        if expanded.endswith("/"):
            parent = path
            prefix = ""
        else:
            parent = path.parent
            prefix = path.name

        try:
            if not parent.exists():
                return

            for entry in sorted(parent.iterdir()):
                name = entry.name
                if not name.startswith(prefix):
                    continue
                if name.startswith(".") and not prefix.startswith("."):
                    continue

                if path_text.startswith("~"):
                    if expanded.endswith("/"):
                        completion_text = path_text + name
                    else:
                        completion_text = (
                            path_text.rsplit("/", 1)[0] + "/" + name
                            if "/" in path_text
                            else "~/" + name
                        )
                else:
                    if expanded.endswith("/"):
                        completion_text = path_text + name
                    else:
                        completion_text = str(path.parent / name) if "/" in path_text else name

                display = name + "/" if entry.is_dir() else name
                if entry.is_dir():
                    completion_text += "/"

                yield Completion(
                    completion_text,
                    start_position=start_pos,
                    display=display,
                    display_meta="dir" if entry.is_dir() else "file",
                )
        except OSError:
            return


class MonitorCompleter(Completer):
    """Context-aware completer for ``rho-agent monitor`` commands, sessions, and approvals."""

    def __init__(self, session_store: Any) -> None:
        self._store = session_store

    def get_completions(self, document: Document, complete_event: Any) -> Iterable[Completion]:
        text = document.text_before_cursor
        stripped = text.lstrip()
        parts = stripped.split()

        # First token: command name
        if not parts or (len(parts) == 1 and not text.endswith(" ")):
            prefix = parts[0].lower() if parts else ""
            for cmd, desc in MONITOR_COMMANDS:
                if cmd.startswith(prefix):
                    yield Completion(
                        cmd,
                        start_position=-len(prefix),
                        display=cmd,
                        display_meta=desc,
                    )
            return

        cmd = parts[0].lower()
        arg_prefix = "" if text.endswith(" ") else parts[-1]

        # Only complete the first argument (target ID)
        arg_index = len(parts) if text.endswith(" ") else len(parts) - 1
        if arg_index != 1:
            return

        if cmd in _SESSION_TARGET_CMDS:
            if cmd in ("pause", "resume", "cancel") and "all".startswith(arg_prefix.lower()):
                yield Completion(
                    "all",
                    start_position=-len(arg_prefix),
                    display="all",
                    display_meta="every live session",
                )
            try:
                sessions = self._store.list(limit=30)
            except Exception:
                sessions = []

            def _iter_tree(items: list[Any]) -> Iterable[Any]:
                for s in items:
                    yield s
                    yield from _iter_tree(getattr(s, "children", []))

            for info in _iter_tree(sessions):
                if info.id.startswith(arg_prefix):
                    preview = info.display_preview[:36] if info.display_preview else info.model
                    meta = f"{info.status} · {preview}" if preview else info.status
                    yield Completion(
                        info.id,
                        start_position=-len(arg_prefix),
                        display=info.id,
                        display_meta=meta,
                    )

        elif cmd == "theme":
            for name, preset in THEMES.items():
                if name.startswith(arg_prefix.lower()):
                    yield Completion(
                        name,
                        start_position=-len(arg_prefix),
                        display=name,
                        display_meta=preset.description,
                    )

        elif cmd in _APPROVAL_TARGET_CMDS:
            if cmd == "approve" and "all".startswith(arg_prefix.lower()):
                yield Completion(
                    "all",
                    start_position=-len(arg_prefix),
                    display="all",
                    display_meta="approve all pending requests",
                )
            try:
                pending = self._store.pending_approvals()
            except Exception:
                pending = []
            for info, req in pending:
                if req.id.startswith(arg_prefix):
                    yield Completion(
                        req.id,
                        start_position=-len(arg_prefix),
                        display=req.id,
                        display_meta=f"{info.id} · {req.tool_name}",
                    )


def create_completer(working_dir: str | None = None) -> Completer:
    """Create a merged completer for commands and paths."""
    return merge_completers([SlashCommandCompleter(), InlinePathCompleter(working_dir=working_dir)])
