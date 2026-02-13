from __future__ import annotations

from datetime import datetime, timezone

from rho_agent.command_center.models import AgentStatus
from rho_agent.command_center.services.local_signal_transport import LocalSignalTransport
from rho_agent.signals import AgentInfo, SignalManager


def _register(sm: SignalManager, session_id: str, model: str = "gpt-5-mini") -> None:
    sm.register(
        AgentInfo(
            session_id=session_id,
            pid=12345,
            model=model,
            instruction_preview="interactive session",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
    )


def test_list_running_includes_pause_state(tmp_path) -> None:
    sm = SignalManager(signal_dir=tmp_path)
    transport = LocalSignalTransport(sm)

    _register(sm, "abc11111-0000-0000-0000-000000000000")
    _register(sm, "def22222-0000-0000-0000-000000000000")
    assert sm.pause("def22222-0000-0000-0000-000000000000")

    running = transport.list_running()

    by_id = {agent.session_id: agent for agent in running}
    assert by_id["abc11111-0000-0000-0000-000000000000"].status == AgentStatus.RUNNING
    assert by_id["def22222-0000-0000-0000-000000000000"].status == AgentStatus.PAUSED


def test_control_actions_delegate_to_signal_manager(tmp_path) -> None:
    sm = SignalManager(signal_dir=tmp_path)
    transport = LocalSignalTransport(sm)
    session_id = "abc11111-0000-0000-0000-000000000000"
    _register(sm, session_id)

    assert transport.pause(session_id)
    assert sm.is_paused(session_id)

    assert transport.resume(session_id)
    assert not sm.is_paused(session_id)

    assert transport.directive(session_id, "review logs")
    assert sm.consume_directives(session_id) == ["review logs"]

    assert transport.kill(session_id)
    assert sm.is_cancelled(session_id)


def test_register_and_deregister_launcher_session(tmp_path) -> None:
    sm = SignalManager(signal_dir=tmp_path)
    transport = LocalSignalTransport(sm)
    session_id = "abc11111-0000-0000-0000-000000000000"

    transport.register_launcher_session(
        session_id,
        pid=999,
        model="gpt-5-mini",
        instruction_preview="hello",
    )

    agents = sm.list_running()
    assert any(agent.session_id == session_id for agent in agents)

    transport.deregister(session_id)
    assert all(agent.session_id != session_id for agent in sm.list_running())
