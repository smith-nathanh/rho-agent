from __future__ import annotations

from dataclasses import dataclass

from textual.widgets import ListItem, ListView, Static

from rho_agent.command_center.services.control_plane import ControlPlane


@dataclass(slots=True)
class _Row:
    session_id: str
    label: str


class AgentList(ListView):
    """Roster pane listing running agents.

    This widget is intentionally lightweight; it pulls data from ControlPlane and
    stores a list of session IDs for selection.
    """

    services: object | None = None

    def __init__(self, *children, **kwargs):
        super().__init__(*children, **kwargs)
        self._rows: list[_Row] = []

    @property
    def control_plane(self) -> ControlPlane | None:
        if self.services is None:
            return None
        return getattr(self.services, "control_plane", None)

    def refresh_running(self) -> None:
        cp = self.control_plane
        if cp is None:
            return

        prior_selected = self.selected_session_id
        agents = cp.list_running()
        self._rows = [
            _Row(session_id=a.session_id, label=f"{a.session_id[:8]}  {a.status.value}")
            for a in agents
        ]

        # Rebuild list items.
        self.clear()
        for row in self._rows:
            self.append(ListItem(Static(row.label)))

        if not self._rows:
            return
        if prior_selected:
            for idx, row in enumerate(self._rows):
                if row.session_id == prior_selected:
                    self.index = idx
                    return
        if self.index is None:
            self.index = 0

    @property
    def selected_session_id(self) -> str | None:
        if self.index is None:
            return None
        if self.index < 0 or self.index >= len(self._rows):
            return None
        return self._rows[self.index].session_id

    @property
    def selected_session_prefix(self) -> str | None:
        sid = self.selected_session_id
        return sid[:8] if sid else None

    def move_selection(self, delta: int) -> None:
        if not self._rows:
            return
        idx = self.index or 0
        idx = max(0, min(len(self._rows) - 1, idx + delta))
        self.index = idx
