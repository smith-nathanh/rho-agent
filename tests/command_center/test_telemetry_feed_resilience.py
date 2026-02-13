from __future__ import annotations

from typing import Any

import pytest

from rho_agent.command_center.services.telemetry_feed import FeedCursor, TelemetryFeed


class FailingStorage:
    def list_sessions(self, *, status: str | None = None, limit: int = 50):  # pragma: no cover
        raise OSError("db unavailable")

    def get_session_detail(self, session_id: str):
        raise OSError("db unavailable")

    def count_sessions(self, status: str | None = None):  # pragma: no cover
        raise OSError("db unavailable")


def test_poll_session_updates_db_unavailable_raises_clear_error() -> None:
    feed = TelemetryFeed(FailingStorage())

    with pytest.raises(OSError, match="db unavailable"):
        feed.poll_session_updates("session-1", FeedCursor(turn_count=0))


def test_poll_session_updates_clamps_cursor_when_turns_shorter() -> None:
    class Storage:
        def get_session_detail(self, session_id: str):
            return type(
                "Detail",
                (),
                {
                    "turns": [
                        {"role": "assistant", "content": "hi", "tool_executions": [{"name": "bash"}]}
                    ]
                },
            )()

    feed = TelemetryFeed(Storage())
    delta = feed.poll_session_updates("s", FeedCursor(turn_count=999))
    assert delta.events == []
    assert delta.cursor.turn_count == 1
