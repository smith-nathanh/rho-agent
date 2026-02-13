from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from rho_agent.command_center.services.telemetry_feed import FeedCursor, TelemetryFeed
from rho_agent.observability.storage.sqlite import SessionDetail, SessionSummary


class FakeTelemetryStorage:
    def __init__(self, detail: SessionDetail | None):
        self._detail = detail

    def list_sessions(self, *, status: str | None = None, limit: int = 50):
        return []

    def count_sessions(self, status: str | None = None) -> int:
        return 0

    def get_session_detail(self, session_id: str):
        return self._detail


def _make_detail(turn_count: int) -> SessionDetail:
    now = datetime.now(timezone.utc)
    turns = []
    for i in range(turn_count):
        turns.append(
            {
                "turn_id": f"t{i}",
                "turn_index": i,
                "started_at": now.isoformat(),
                "ended_at": None,
                "input_tokens": i + 1,
                "output_tokens": i + 2,
                "reasoning_tokens": 0,
                "context_size": 0,
                "user_input": f"hello {i}",
                "tool_executions": [
                    {
                        "execution_id": f"e{i}",
                        "tool_name": "tool",
                        "arguments": {"i": i},
                        "result": "ok",
                        "success": True,
                        "error": None,
                        "duration_ms": 1,
                        "started_at": now.isoformat(),
                    }
                ],
            }
        )
    return SessionDetail(
        session_id="s1",
        team_id="team",
        project_id="proj",
        agent_id=None,
        environment=None,
        profile=None,
        model="m",
        started_at=now,
        ended_at=None,
        status="active",
        total_input_tokens=0,
        total_output_tokens=0,
        total_reasoning_tokens=0,
        total_tool_calls=0,
        context_size=0,
        metadata={},
        turns=turns,
    )


def test_poll_session_updates_initial_cursor_emits_all_turns_and_tools():
    storage = FakeTelemetryStorage(_make_detail(turn_count=2))
    feed = TelemetryFeed(storage)

    delta = feed.poll_session_updates("s1", cursor=None)

    # Each turn emits a 'turn' event and then its tool executions
    assert [e.type for e in delta.events] == [
        "turn",
        "tool_execution",
        "turn",
        "tool_execution",
    ]
    assert delta.cursor.turn_count == 2


def test_poll_session_updates_only_emits_new_turns_after_cursor():
    storage = FakeTelemetryStorage(_make_detail(turn_count=3))
    feed = TelemetryFeed(storage)

    delta = feed.poll_session_updates("s1", cursor=FeedCursor(turn_count=2))

    assert [e.type for e in delta.events] == ["turn", "tool_execution"]
    assert delta.events[0].payload["turn_index"] == 2
    assert delta.cursor.turn_count == 3


def test_poll_session_updates_clamps_cursor_beyond_end():
    storage = FakeTelemetryStorage(_make_detail(turn_count=1))
    feed = TelemetryFeed(storage)

    delta = feed.poll_session_updates("s1", cursor=FeedCursor(turn_count=100))

    assert delta.events == []
    assert delta.cursor.turn_count == 1
