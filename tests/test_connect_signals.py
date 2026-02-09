from __future__ import annotations

from datetime import datetime, timezone

from rho_agent.signals import AgentInfo, SignalManager


def _register(sm: SignalManager, session_id: str) -> None:
    sm.register(
        AgentInfo(
            session_id=session_id,
            pid=12345,
            model="gpt-5-mini",
            instruction_preview="interactive",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
    )


def test_export_request_and_ready_lifecycle(tmp_path) -> None:
    sm = SignalManager(signal_dir=tmp_path)
    session_id = "abc12345-0000-0000-0000-000000000000"
    _register(sm, session_id)

    assert sm.request_export(session_id)
    assert sm.has_export_request(session_id)
    assert not sm.export_ready(session_id)

    sm.context_path(session_id).write_text("content", encoding="utf-8")
    assert sm.export_ready(session_id)

    sm.clear_export_request(session_id)
    assert not sm.has_export_request(session_id)


def test_clear_export_removes_export_and_context(tmp_path) -> None:
    sm = SignalManager(signal_dir=tmp_path)
    session_id = "abc12345-0000-0000-0000-000000000000"
    _register(sm, session_id)

    assert sm.request_export(session_id)
    sm.context_path(session_id).write_text("content", encoding="utf-8")
    sm.clear_export(session_id)

    assert not sm.has_export_request(session_id)
    assert not sm.export_ready(session_id)


def test_deregister_cleans_export_and_context_files(tmp_path) -> None:
    sm = SignalManager(signal_dir=tmp_path)
    session_id = "abc12345-0000-0000-0000-000000000000"
    _register(sm, session_id)
    assert sm.request_export(session_id)
    sm.context_path(session_id).write_text("content", encoding="utf-8")

    sm.deregister(session_id)

    assert not sm.has_export_request(session_id)
    assert not sm.export_ready(session_id)


def test_request_export_requires_running_session(tmp_path) -> None:
    sm = SignalManager(signal_dir=tmp_path)
    session_id = "abc12345-0000-0000-0000-000000000000"

    assert not sm.request_export(session_id)
