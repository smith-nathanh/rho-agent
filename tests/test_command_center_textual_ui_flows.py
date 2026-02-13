from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("textual")

from rho_agent.command_center.app import CommandCenterApp, CommandCenterServices
from rho_agent.command_center.models import AgentStatus, LaunchRequest, RunningAgent


@dataclass(slots=True)
class _FakeControlPlane:
    running: list[RunningAgent]

    def list_running(self) -> list[RunningAgent]:
        return list(self.running)

    def resolve_running_prefix(self, prefix: str) -> list[str]:
        if prefix == "all":
            return [a.session_id for a in self.running]
        return [a.session_id for a in self.running if a.session_id.startswith(prefix)]

    def resolve_single_running(self, prefix: str):
        matches = self.resolve_running_prefix(prefix)
        if not matches:
            return None, f"No running agents matching '{prefix}'"
        if len(matches) > 1:
            return None, f"Prefix '{prefix}' matched multiple sessions; use a longer prefix."
        return matches[0], None

    def pause(self, prefix: str):
        self.last_call = ("pause", prefix)

    def resume(self, prefix: str):
        self.last_call = ("resume", prefix)

    def kill(self, prefix: str):
        self.last_call = ("kill", prefix)

    def directive(self, prefix: str, text: str):
        self.last_call = ("directive", prefix, text)


@dataclass(slots=True)
class _FakeTelemetryFeed:
    refreshed: int = 0


@dataclass(slots=True)
class _FakeLauncher:
    launched: list[LaunchRequest]

    def launch(self, request: LaunchRequest):
        self.launched.append(request)
        return None


@pytest.mark.asyncio
async def test_roster_selection_and_keybindings_dispatch(tmp_path: Path) -> None:
    agents = [
        RunningAgent(
            session_id="abcdef0123456789",
            pid=1,
            model="m",
            instruction_preview="",
            started_at=None,
            status=AgentStatus.RUNNING,
        ),
        RunningAgent(
            session_id="12345678deadbeef",
            pid=2,
            model="m",
            instruction_preview="",
            started_at=None,
            status=AgentStatus.PAUSED,
        ),
    ]
    cp = _FakeControlPlane(running=agents)
    launcher = _FakeLauncher(launched=[])
    feed = _FakeTelemetryFeed()

    app = CommandCenterApp(
        services=CommandCenterServices(control_plane=cp, telemetry_feed=feed, launcher=launcher)
    )

    async with app.run_test() as pilot:
        await pilot.pause(0.05)

        roster = pilot.app.query_one("#roster")
        assert roster.selected_session_prefix == "abcdef01"

        await pilot.press("j")
        assert roster.selected_session_prefix == "12345678"

        await pilot.press("p")
        assert cp.last_call == ("pause", "12345678")

        await pilot.press("r")
        assert cp.last_call == ("resume", "12345678")

        await pilot.press("x")
        assert cp.last_call == ("kill", "12345678")


@pytest.mark.asyncio
async def test_launch_modal_validation_and_submit(tmp_path: Path) -> None:
    cp = _FakeControlPlane(
        running=[
            RunningAgent(
                session_id="abcdef0123456789",
                pid=1,
                model="m",
                instruction_preview="",
                started_at=None,
                status=AgentStatus.RUNNING,
            )
        ]
    )
    launcher = _FakeLauncher(launched=[])
    feed = _FakeTelemetryFeed()

    app = CommandCenterApp(
        services=CommandCenterServices(control_plane=cp, telemetry_feed=feed, launcher=launcher)
    )

    async with app.run_test() as pilot:
        await pilot.pause(0.05)

        await pilot.press("l")
        await pilot.pause(0.05)

        # Invalid path should block submission.
        pilot.app.query_one("#working-dir").value = str(tmp_path / "does-not-exist")
        await pilot.click("#launch")
        await pilot.pause(0.05)
        assert pilot.app.screen.query_one("#error").renderable.plain == "Working dir must exist"
        assert launcher.launched == []

        # Valid path should submit.
        pilot.app.query_one("#working-dir").value = str(tmp_path)
        pilot.app.query_one("#profile").value = "readonly"
        pilot.app.query_one("#model").value = "gpt-5-mini"
        pilot.app.query_one("#prompt").value = ""
        await pilot.click("#launch")
        await pilot.pause(0.05)

        assert len(launcher.launched) == 1
        req = launcher.launched[0]
        assert req.working_dir == tmp_path
        assert req.profile == "readonly"
        assert req.model == "gpt-5-mini"


@pytest.mark.asyncio
async def test_command_palette_dispatch_and_errors(tmp_path: Path) -> None:
    agents = [
        RunningAgent(
            session_id="abcdef0123456789",
            pid=1,
            model="m",
            instruction_preview="",
            started_at=None,
            status=AgentStatus.RUNNING,
        ),
        RunningAgent(
            session_id="abc9999922221111",
            pid=2,
            model="m",
            instruction_preview="",
            started_at=None,
            status=AgentStatus.RUNNING,
        ),
    ]
    cp = _FakeControlPlane(running=agents)
    launcher = _FakeLauncher(launched=[])
    feed = _FakeTelemetryFeed()

    app = CommandCenterApp(
        services=CommandCenterServices(control_plane=cp, telemetry_feed=feed, launcher=launcher)
    )

    async with app.run_test() as pilot:
        await pilot.pause(0.05)

        # Dispatch a pause via palette.
        await pilot.press("/")
        cmd_input = pilot.app.query_one("#command-input")
        cmd_input.value = "/pause abcdef01"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert cp.last_call == ("pause", "abcdef01")

        # Unknown command shouldn't crash; should not change last_call.
        await pilot.press("/")
        cmd_input = pilot.app.query_one("#command-input")
        cmd_input.value = "/doesnotexist"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert cp.last_call == ("pause", "abcdef01")

        # Ambiguous directive prefix should error (two matches for 'abc').
        await pilot.press("/")
        cmd_input = pilot.app.query_one("#command-input")
        cmd_input.value = "/directive abc hello"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert cp.last_call == ("pause", "abcdef01")
