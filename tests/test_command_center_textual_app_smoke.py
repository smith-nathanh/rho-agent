from __future__ import annotations

import pytest

pytest.importorskip("textual")

from rho_agent.command_center.app import CommandCenterApp


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
