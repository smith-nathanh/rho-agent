from __future__ import annotations

from textual.widgets import Static


class DetailPanel(Static):
    """Details pane.

    Placeholder: will later show session metadata + current state.
    """

    services: object | None = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session_id: str | None = None
        self.update("Details")

    def set_session(self, session_id: str | None) -> None:
        self._session_id = session_id
        if session_id is None:
            self.update("Details\n\n(no selection)")
        else:
            self.update(f"Details\n\nSession: {session_id[:8]}")
