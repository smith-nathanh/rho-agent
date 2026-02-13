from __future__ import annotations

import pytest

pytest.importorskip("textual")

from rho_agent.command_center.app import CommandCenterApp
from rho_agent.command_center.app import CommandCenterServices


class _NoAgentsControlPlane:
    def list_running(self):
        return []


class _NoopTelemetryFeed:
    pass


class _NoopLauncher:
    pass


@pytest.mark.asyncio
async def test_command_center_app_smoke() -> None:
    """App composes and basic actions/keybindings are present.

    We don't try to validate rendering; just ensure the layout mounts and actions
    can be invoked without crashing.
    """

    app = CommandCenterApp()

    async with app.run_test() as pilot:
        # Let mount / refresh complete.
        await pilot.pause(0.05)

        # Ensure app registered core actions.
        for action in (
            "pause",
            "resume",
            "kill",
            "directive",
            "refresh",
            "roster_down",
            "roster_up",
            "command_palette",
        ):
            assert hasattr(app, f"action_{action}")

        # Invoke a few actions.
        await pilot.app.action_refresh()
        await pilot.app.action_roster_down()
        await pilot.app.action_roster_up()
        await pilot.app.action_command_palette()


@pytest.mark.asyncio
async def test_command_input_focused_when_no_running_agents() -> None:
    app = CommandCenterApp(
        services=CommandCenterServices(
            control_plane=_NoAgentsControlPlane(),
            telemetry_feed=_NoopTelemetryFeed(),
            launcher=_NoopLauncher(),
        )
    )

    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        assert pilot.app.focused is pilot.app.query_one("#command-input")


@pytest.mark.asyncio
async def test_command_panel_does_not_overlap_footer() -> None:
    app = CommandCenterApp(
        services=CommandCenterServices(
            control_plane=_NoAgentsControlPlane(),
            telemetry_feed=_NoopTelemetryFeed(),
            launcher=_NoopLauncher(),
        )
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.05)
        command_region = pilot.app.query_one("#command").region
        input_region = pilot.app.query_one("#command-input").region
        footer_region = pilot.app.query_one("Footer").region
        assert command_region.y + command_region.height <= footer_region.y
        assert input_region.y + input_region.height <= footer_region.y
