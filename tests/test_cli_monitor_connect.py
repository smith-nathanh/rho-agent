from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from rho_agent.cli.monitor import monitor
from rho_agent.signals import AgentInfo


class FakeTelemetryStorage:
    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def list_sessions(self, status: str | None = None, limit: int = 20) -> list[object]:
        del status, limit
        return []

    def count_sessions(self, status: str | None = None) -> int:
        del status
        return 0

    def get_session_detail(self, session_id: str) -> object | None:
        del session_id
        return None


class FakeSignalManager:
    def __init__(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._session_ids = [
            "aaa11111-0000-0000-0000-000000000000",
            "bbb22222-0000-0000-0000-000000000000",
        ]
        self._running = [
            AgentInfo(
                session_id=sid,
                pid=1000 + i,
                model="gpt-5-mini",
                instruction_preview="interactive",
                started_at=now,
            )
            for i, sid in enumerate(self._session_ids)
        ]
        self._export_ready: set[str] = set()
        self._response_seq = {sid: 0 for sid in self._session_ids}
        self._response_text = {sid: "" for sid in self._session_ids}
        self.queued_directives: list[tuple[str, str]] = []
        self.clear_export_calls: list[str] = []

    def list_running(self) -> list[AgentInfo]:
        return list(self._running)

    def is_paused(self, session_id: str) -> bool:
        del session_id
        return False

    def get_last_response(self, session_id: str) -> tuple[int, str] | None:
        seq = self._response_seq[session_id]
        text = self._response_text[session_id]
        if seq == 0:
            return None
        return seq, text

    def queue_directive(self, session_id: str, directive: str) -> bool:
        self.queued_directives.append((session_id, directive))
        if directive.startswith("Task: "):
            self._response_seq[session_id] += 1
            self._response_text[session_id] = f"response from {session_id[:8]}"
        return True

    def clear_export(self, session_id: str) -> None:
        self.clear_export_calls.append(session_id)
        self._export_ready.discard(session_id)

    def request_export(self, session_id: str) -> bool:
        self._export_ready.add(session_id)
        return True

    def export_ready(self, session_id: str) -> bool:
        return session_id in self._export_ready

    def context_path(self, session_id: str) -> Path:
        return Path(f"/tmp/{session_id}.context")


def test_monitor_connect_then_disconnect(monkeypatch) -> None:
    fake_sm = FakeSignalManager()
    monkeypatch.setattr("rho_agent.cli.monitor.SignalManager", lambda: fake_sm)
    monkeypatch.setattr("rho_agent.cli.monitor.TelemetryStorage", FakeTelemetryStorage)

    commands = iter(
        [
            "connect aaa bbb -- investigate build failure",
            "disconnect",
            "quit",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(commands))

    monitor(db_path=":memory:")

    task_directives = [d for sid, d in fake_sm.queued_directives if d.startswith("Task: ")]
    assert len(task_directives) == 2
    assert "Peer context files:" in task_directives[0]
    assert "/tmp/bbb22222-0000-0000-0000-000000000000.context" in task_directives[0]
    assert "Prior agent responses in this connect session:\n(none yet)" in task_directives[0]
    assert "[aaa11111]\nresponse from aaa11111" in task_directives[1]

    resume_directives = [
        d for sid, d in fake_sm.queued_directives if d == "The connect session has ended. Resume your previous work."
    ]
    assert len(resume_directives) == 2
    assert fake_sm.clear_export_calls
    assert "aaa11111-0000-0000-0000-000000000000" in fake_sm.clear_export_calls
    assert "bbb22222-0000-0000-0000-000000000000" in fake_sm.clear_export_calls


def test_monitor_disconnect_without_active_connect(monkeypatch) -> None:
    fake_sm = FakeSignalManager()
    monkeypatch.setattr("rho_agent.cli.monitor.SignalManager", lambda: fake_sm)
    monkeypatch.setattr("rho_agent.cli.monitor.TelemetryStorage", FakeTelemetryStorage)

    commands = iter(["disconnect", "quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(commands))

    printed: list[str] = []

    def capture_print(*args: object, **kwargs: object) -> None:
        del kwargs
        printed.extend(str(arg) for arg in args)

    monkeypatch.setattr("rho_agent.cli.monitor.console.print", capture_print)

    monitor(db_path=":memory:")

    assert any("No active connect session." in line for line in printed)
