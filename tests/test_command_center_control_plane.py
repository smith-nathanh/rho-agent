from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from rho_agent.command_center.models import AgentStatus, RunningAgent
from rho_agent.command_center.services.control_plane import ControlPlane


@dataclass
class FakeTransport:
    agents: list[RunningAgent]

    def __post_init__(self) -> None:
        self.paused: list[str] = []
        self.resumed: list[str] = []
        self.killed: list[str] = []
        self.directives: list[tuple[str, str]] = []

    def list_running(self) -> list[RunningAgent]:
        return list(self.agents)

    def pause(self, session_id: str) -> bool:
        self.paused.append(session_id)
        return True

    def resume(self, session_id: str) -> bool:
        self.resumed.append(session_id)
        return True

    def kill(self, session_id: str) -> bool:
        self.killed.append(session_id)
        return True

    def directive(self, session_id: str, text: str) -> bool:
        self.directives.append((session_id, text))
        return True

    def register_launcher_session(
        self,
        session_id: str,
        *,
        pid: int,
        model: str,
        instruction_preview: str,
    ) -> None:
        del session_id, pid, model, instruction_preview

    def deregister(self, session_id: str) -> None:
        del session_id


def _agent(session_id: str) -> RunningAgent:
    return RunningAgent(
        session_id=session_id,
        pid=1234,
        model="gpt-5-mini",
        instruction_preview="interactive",
        started_at=datetime.now(timezone.utc),
        status=AgentStatus.RUNNING,
    )


def test_resolve_single_running_reports_ambiguous_prefix() -> None:
    transport = FakeTransport(
        agents=[
            _agent("abc11111-0000-0000-0000-000000000000"),
            _agent("abc22222-0000-0000-0000-000000000000"),
        ]
    )
    control = ControlPlane(transport)

    session_id, error = control.resolve_single_running("abc")

    assert session_id is None
    assert error is not None
    assert "multiple sessions" in error


def test_pause_by_prefix_targets_all_matches() -> None:
    transport = FakeTransport(
        agents=[
            _agent("abc11111-0000-0000-0000-000000000000"),
            _agent("abc22222-0000-0000-0000-000000000000"),
            _agent("def33333-0000-0000-0000-000000000000"),
        ]
    )
    control = ControlPlane(transport)

    outcome = control.pause("abc")

    assert outcome.error is None
    assert outcome.acted_session_ids == [
        "abc11111-0000-0000-0000-000000000000",
        "abc22222-0000-0000-0000-000000000000",
    ]
    assert transport.paused == outcome.acted_session_ids


def test_kill_all_targets_all_running_sessions() -> None:
    transport = FakeTransport(
        agents=[
            _agent("abc11111-0000-0000-0000-000000000000"),
            _agent("def22222-0000-0000-0000-000000000000"),
        ]
    )
    control = ControlPlane(transport)

    outcome = control.kill("all")

    assert outcome.error is None
    assert outcome.acted_session_ids == [
        "abc11111-0000-0000-0000-000000000000",
        "def22222-0000-0000-0000-000000000000",
    ]
    assert transport.killed == outcome.acted_session_ids


def test_directive_requires_unambiguous_prefix() -> None:
    transport = FakeTransport(
        agents=[
            _agent("abc11111-0000-0000-0000-000000000000"),
            _agent("abc22222-0000-0000-0000-000000000000"),
        ]
    )
    control = ControlPlane(transport)

    outcome = control.directive("abc", "review logs")

    assert not outcome.acted_session_ids
    assert outcome.warning is not None
    assert not transport.directives


def test_directive_queues_for_single_match() -> None:
    transport = FakeTransport(agents=[_agent("abc11111-0000-0000-0000-000000000000")])
    control = ControlPlane(transport)

    outcome = control.directive("abc", "review logs")

    assert outcome.error is None
    assert outcome.acted_session_ids == ["abc11111-0000-0000-0000-000000000000"]
    assert transport.directives == [
        ("abc11111-0000-0000-0000-000000000000", "review logs")
    ]
