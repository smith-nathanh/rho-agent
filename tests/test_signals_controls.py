from __future__ import annotations

from datetime import datetime, timezone

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


def test_pause_and_resume_by_prefix(tmp_path) -> None:
    sm = SignalManager(signal_dir=tmp_path)
    _register(sm, "abc11111-0000-0000-0000-000000000000")
    _register(sm, "abc22222-0000-0000-0000-000000000000")
    _register(sm, "def33333-0000-0000-0000-000000000000")

    paused = sm.pause_by_prefix("abc")
    assert len(paused) == 2
    assert sm.is_paused("abc11111-0000-0000-0000-000000000000")
    assert sm.is_paused("abc22222-0000-0000-0000-000000000000")
    assert not sm.is_paused("def33333-0000-0000-0000-000000000000")

    resumed = sm.resume_by_prefix("abc2")
    assert resumed == ["abc22222-0000-0000-0000-000000000000"]
    assert sm.is_paused("abc11111-0000-0000-0000-000000000000")
    assert not sm.is_paused("abc22222-0000-0000-0000-000000000000")


def test_queue_and_consume_directives(tmp_path) -> None:
    sm = SignalManager(signal_dir=tmp_path)
    session_id = "abc12345-0000-0000-0000-000000000000"
    _register(sm, session_id)

    assert sm.queue_directive(session_id, "first directive")
    assert sm.queue_directive(session_id, "second directive")

    directives = sm.consume_directives(session_id)
    assert directives == ["first directive", "second directive"]

    # Consuming again should be empty because the queue is cleared.
    assert sm.consume_directives(session_id) == []


def test_deregister_cleans_pause_and_directive_files(tmp_path) -> None:
    sm = SignalManager(signal_dir=tmp_path)
    session_id = "abc12345-0000-0000-0000-000000000000"
    _register(sm, session_id)
    assert sm.pause(session_id)
    assert sm.queue_directive(session_id, "directive")
    assert sm.record_response(session_id, "latest reply")

    sm.deregister(session_id)

    assert not sm.is_paused(session_id)
    assert sm.consume_directives(session_id) == []
    assert sm.get_last_response(session_id) is None


def test_record_and_read_last_response(tmp_path) -> None:
    sm = SignalManager(signal_dir=tmp_path)
    session_id = "abc12345-0000-0000-0000-000000000000"
    _register(sm, session_id)

    assert sm.record_response(session_id, "first response")
    assert sm.get_last_response(session_id) == (1, "first response")

    assert sm.record_response(session_id, "second response")
    assert sm.get_last_response(session_id) == (2, "second response")


def test_record_response_requires_running_session(tmp_path) -> None:
    sm = SignalManager(signal_dir=tmp_path)
    session_id = "abc12345-0000-0000-0000-000000000000"

    assert not sm.record_response(session_id, "reply")
    assert sm.get_last_response(session_id) is None
