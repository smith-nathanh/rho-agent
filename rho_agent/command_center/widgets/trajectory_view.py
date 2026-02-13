from __future__ import annotations

from textual.widgets import Static


class TrajectoryView(Static):
    """Trajectory pane.

    Placeholder: will later render incremental telemetry (turns/tool calls) for the
    selected session.
    """

    services: object | None = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session_id: str | None = None
        self.update("Trajectory")

    def set_session(self, session_id: str | None) -> None:
        self._session_id = session_id
        if session_id is None:
            self.update("Trajectory\n\n(no selection)")
        else:
            self.update(f"Trajectory\n\nSession: {session_id[:8]}")
