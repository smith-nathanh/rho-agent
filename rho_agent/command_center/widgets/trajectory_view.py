from __future__ import annotations

from rho_agent.command_center.services.telemetry_feed import FeedCursor, TelemetryFeed
from textual.widgets import Static


class TrajectoryView(Static):
    """Trajectory pane.

    Renders incremental telemetry (turns/tool calls) for the selected session.
    """

    services: object | None = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session_id: str | None = None
        self._cursor = FeedCursor(turn_count=0)
        self._lines: list[str] = []
        self.update("Trajectory\n\n(no selection)")

    @property
    def telemetry_feed(self) -> TelemetryFeed | None:
        if self.services is None:
            return None
        feed = getattr(self.services, "telemetry_feed", None)
        if feed is None:
            return None
        if not hasattr(feed, "get_session_detail") or not hasattr(feed, "poll_session_updates"):
            return None
        return feed

    def set_session(self, session_id: str | None) -> None:
        if session_id != self._session_id:
            self._cursor = FeedCursor(turn_count=0)
            self._lines = []
        self._session_id = session_id
        if session_id is None:
            self.update("Trajectory\n\n(no selection)")
        else:
            self._refresh_view()

    def on_mount(self) -> None:
        self.set_interval(0.5, self._poll)

    def _poll(self) -> None:
        session_id = self._session_id
        feed = self.telemetry_feed
        if session_id is None or feed is None:
            return

        delta = feed.poll_session_updates(session_id, self._cursor)
        self._cursor = delta.cursor
        for event in delta.events:
            if event.type == "turn":
                idx = event.payload.get("turn_index", "?")
                user_input = (event.payload.get("user_input") or "").strip()
                preview = user_input[:80] + ("..." if len(user_input) > 80 else "")
                self._lines.append(f"turn {idx}: {preview or '(no user input recorded)'}")
            elif event.type == "tool_execution":
                tool_name = event.payload.get("tool_name", "?")
                ok = bool(event.payload.get("success", False))
                status = "ok" if ok else "error"
                self._lines.append(f"  tool {tool_name}: {status}")
        self._refresh_view()

    def _refresh_view(self) -> None:
        session_id = self._session_id
        if session_id is None:
            self.update("Trajectory\n\n(no selection)")
            return

        header = f"Trajectory\n\nSession: {session_id[:8]}"
        if self._lines:
            # Keep last ~200 lines to avoid unbounded growth.
            self._lines = self._lines[-200:]
            self.update(header + "\n\n" + "\n".join(self._lines))
            return

        feed = self.telemetry_feed
        if feed is None:
            self.update(header + "\n\n(telemetry feed unavailable)")
            return

        detail = feed.get_session_detail(session_id)
        if detail is None:
            self.update(
                header
                + "\n\n(no telemetry for this session yet)\nHint: launch with --team-id and --project-id."
            )
            return

        if not detail.turns:
            self.update(header + "\n\n(waiting for first turn...)")
            return

        # Session has turns but no incremental lines yet; prime from detail.
        for turn in detail.turns:
            idx = turn.get("turn_index", "?")
            user_input = (turn.get("user_input") or "").strip()
            preview = user_input[:80] + ("..." if len(user_input) > 80 else "")
            self._lines.append(f"turn {idx}: {preview or '(no user input recorded)'}")
            for tool in turn.get("tool_executions", []) or []:
                tool_name = tool.get("tool_name", "?")
                ok = bool(tool.get("success", False))
                status = "ok" if ok else "error"
                self._lines.append(f"  tool {tool_name}: {status}")
        self._lines = self._lines[-200:]
        self._cursor = FeedCursor(turn_count=len(detail.turns))
        self.update(header + "\n\n" + "\n".join(self._lines))
