from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rho_agent.command_center.models import LaunchRequest
from rho_agent.command_center.services.launcher import AgentLauncher


@dataclass
class StubPopen:
    args: list[str]
    pid: int = 1234
    _returncode: int | None = None
    terminate_called: bool = False
    kill_called: bool = False
    wait_calls: int = 0

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminate_called = True
        self._returncode = 0

    def kill(self) -> None:
        self.kill_called = True
        self._returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self._returncode is None:
            self._returncode = 0
        return self._returncode


def test_launcher_command_assembly() -> None:
    captured: dict[str, object] = {}

    def popen_factory(args: list[str], **kwargs: object) -> StubPopen:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return StubPopen(args=args, pid=555)

    launcher = AgentLauncher(popen_factory=popen_factory, entrypoint=["rho-agent"])
    req = LaunchRequest(
        working_dir=Path("/tmp"),
        profile="developer",
        model="gpt-5-mini",
        prompt="hello",
        auto_approve=True,
        team_id="team-1",
        project_id="proj-1",
    )

    launched = launcher.launch(req)

    args = captured["args"]
    assert isinstance(args, list)
    # Validate structure and key flags.
    assert args[0:2] == ["rho-agent", "main"]
    assert "--profile" in args and args[args.index("--profile") + 1] == "developer"
    assert "--model" in args and args[args.index("--model") + 1] == "gpt-5-mini"
    assert "--working-dir" in args and args[args.index("--working-dir") + 1] == "/tmp"
    assert "--team-id" in args and args[args.index("--team-id") + 1] == "team-1"
    assert "--project-id" in args and args[args.index("--project-id") + 1] == "proj-1"
    assert "--auto-approve" in args
    assert args[-1] == "hello"
    assert "--session-id" in args
    session_id = args[args.index("--session-id") + 1]
    assert launched.session_id == session_id
    assert launched.pid == 555
    assert launched.command == args


def test_launcher_registry_list_cleanup_and_stop() -> None:
    # Return a new StubPopen each call and keep a handle so we can mark it exited.
    stubs: list[StubPopen] = []

    def popen_factory(args: list[str], **kwargs: object) -> StubPopen:
        proc = StubPopen(args=args, pid=1000 + len(stubs))
        stubs.append(proc)
        return proc

    launcher = AgentLauncher(popen_factory=popen_factory, entrypoint=["rho-agent"])

    a1 = launcher.launch(LaunchRequest(working_dir=Path("."), prompt="one"))
    a2 = launcher.launch(LaunchRequest(working_dir=Path("."), prompt="two"))

    managed = launcher.list_managed()
    assert {m.session_id for m in managed} == {a1.session_id, a2.session_id}

    # Simulate one process exiting; list_managed should clean it up.
    stubs[0]._returncode = 0
    managed = launcher.list_managed()
    assert {m.session_id for m in managed} == {a2.session_id}

    # stop_managed should remove the still-running process.
    assert launcher.stop_managed(a2.session_id) is True
    assert launcher.list_managed() == []

    # Stopping unknown session returns False.
    assert launcher.stop_managed("doesnotexist") is False
