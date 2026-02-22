"""Input completion: path and command completers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from collections.abc import Iterable
from typing import Any

from prompt_toolkit.completion import (
    Completer,
    Completion,
    WordCompleter,
    merge_completers,
)
from prompt_toolkit.document import Document

# Commands the user can type during the session
COMMANDS = [
    "/approve",
    "/compact",
    "/download",
    "/mode",
    "/write",
    "/resume",
    "/help",
    "/clear",
    "exit",
    "quit",
]

# Pattern to detect path-like strings in text
PATH_PATTERN = re.compile(
    r"(~/?|\.{1,2}/|/)?([a-zA-Z0-9_\-./]+/[a-zA-Z0-9_\-.]*|~[a-zA-Z0-9_\-./]*)$"
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

                # Build completion text preserving user's path style
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
                    display_meta="dir" if entry.is_dir() else "",
                )
        except PermissionError:
            return


def create_completer(working_dir: str | None = None) -> Completer:
    """Create a merged completer for commands and paths."""
    command_completer = WordCompleter(COMMANDS, ignore_case=True)
    path_completer = InlinePathCompleter(working_dir=working_dir)
    return merge_completers([command_completer, path_completer])
