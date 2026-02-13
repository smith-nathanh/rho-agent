from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Input, Static


class CommandInput(Static):
    """Command input pane.

    Accepts palette-like commands. The app listens for submitted values.
    """

    services: object | None = None

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static("Command:", id="command-label")
            yield Input(
                placeholder="/pause [prefix], /resume [prefix], /kill [prefix], /directive <prefix> <text>, /launch, /refresh",
                id="command-input",
            )

    def focus_input(self) -> None:
        self.query_one("#command-input", Input).focus()

    def clear(self) -> None:
        self.query_one("#command-input", Input).value = ""
