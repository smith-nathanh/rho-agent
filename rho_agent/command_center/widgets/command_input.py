from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Input, Static


class CommandInput(Static):
    """Command input pane.

    Placeholder for a palette/command line. For now it just accepts text.
    """

    services: object | None = None

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static("Command:", id="command-label")
            yield Input(placeholder="/pause, /resume, /kill, /directive ...", id="command-input")

    def focus_input(self) -> None:
        self.query_one("#command-input", Input).focus()
