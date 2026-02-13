from __future__ import annotations

from dataclasses import dataclass

from rho_agent.command_center.models import LaunchRequest
from rho_agent.command_center.services.launcher import AgentLauncher


@dataclass
class DummyPopen:
    pid: int = 123
    returncode: int | None = None
    waited: bool = False

    def poll(self):
        return self.returncode

    def wait(self, timeout: float | None = None):
        self.waited = True
        # simulate exit after wait
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9


def test_stop_managed_unknown_session_returns_false(tmp_path) -> None:
    launcher = AgentLauncher(popen_factory=lambda *a, **k: DummyPopen())
    assert launcher.stop_managed("nope") is False


def test_stop_managed_exited_process_is_removed(tmp_path, monkeypatch) -> None:
    launcher = AgentLauncher(popen_factory=lambda *a, **k: DummyPopen(returncode=0))

    req = LaunchRequest(
        working_dir=tmp_path,
        profile="readonly",
        model="gpt-5-mini",
        prompt="",
        auto_approve=False,
    )
    launched = launcher.launch(req)

    assert launcher.stop_managed(launched.session_id) is True
    assert launcher.list_managed() == []
