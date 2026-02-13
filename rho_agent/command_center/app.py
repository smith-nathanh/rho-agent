"""Textual command-center app scaffold.

This is an early skeleton intended to support incremental migration from the legacy
prompt-toolkit monitor/dashboard.

The app wires the service layer (ControlPlane + LocalSignalTransport, TelemetryFeed,
AgentLauncher) and exposes placeholder keybindings that dispatch to control-plane
actions without crashing.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input

from rho_agent.command_center.commands import parse_palette_command
from rho_agent.command_center.modals.launch_agent_modal import LaunchAgentModal
from rho_agent.command_center.models import LaunchRequest
from rho_agent.command_center.services.control_plane import ControlPlane
from rho_agent.command_center.services.launcher import AgentLauncher
from rho_agent.command_center.services.local_signal_transport import LocalSignalTransport
from rho_agent.command_center.services.telemetry_feed import TelemetryFeed
from rho_agent.observability.storage.sqlite import SQLiteStorage

from .widgets.agent_list import AgentList
from .widgets.command_input import CommandInput
from .widgets.detail_panel import DetailPanel
from .widgets.trajectory_view import TrajectoryView


@dataclass(slots=True)
class CommandCenterServices:
    control_plane: ControlPlane
    telemetry_feed: TelemetryFeed
    launcher: AgentLauncher


class CommandCenterApp(App[None]):
    """Command-center TUI (Textual) skeleton."""

    CSS_PATH = "layout.tcss"

    BINDINGS = [
        # Quit
        Binding("q", "quit", "Quit"),
        # Roster navigation
        Binding("j", "roster_down", "Down"),
        Binding("k", "roster_up", "Up"),
        Binding("down", "roster_down", show=False),
        Binding("up", "roster_up", show=False),
        # Focus
        Binding("tab", "focus_next", "Next focus", show=False),
        # Control actions
        Binding("p", "pause", "Pause"),
        Binding("r", "resume", "Resume"),
        Binding("x", "kill", "Kill"),
        Binding("d", "directive", "Directive"),
        Binding("n", "refresh", "Refresh"),
        Binding("l", "launch", "Launch"),
        Binding("/", "command_palette", "Command"),
    ]

    def __init__(
        self,
        *,
        services: CommandCenterServices | None = None,
        control_plane: ControlPlane | None = None,
        telemetry_feed: TelemetryFeed | None = None,
        launcher: AgentLauncher | None = None,
    ) -> None:
        super().__init__()

        if services is not None:
            self.services = services
            return

        # Allow passing individual services (useful in tests).
        if control_plane is None:
            control_plane = ControlPlane(LocalSignalTransport())
        if telemetry_feed is None:
            telemetry_feed = TelemetryFeed(SQLiteStorage())
        if launcher is None:
            launcher = AgentLauncher()

        self.services = CommandCenterServices(
            control_plane=control_plane,
            telemetry_feed=telemetry_feed,
            launcher=launcher,
        )

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Vertical(id="root"):
            with Horizontal(id="main"):
                yield AgentList(id="roster")
                yield TrajectoryView(id="trajectory")
                yield DetailPanel(id="details")
            yield CommandInput(id="command")

        yield Footer()

    async def on_mount(self) -> None:
        # Wire services into widgets.
        self.query_one(AgentList).services = self.services
        self.query_one(TrajectoryView).services = self.services
        self.query_one(DetailPanel).services = self.services
        self.query_one(CommandInput).services = self.services

        # Initial state.
        await self.action_refresh()
        self.query_one(AgentList).focus()

    def _selected_prefix(self) -> str:
        roster = self.query_one(AgentList)
        return roster.selected_session_prefix or "all"

    async def action_roster_down(self) -> None:
        self.query_one(AgentList).move_selection(1)

    async def action_roster_up(self) -> None:
        self.query_one(AgentList).move_selection(-1)

    async def action_refresh(self) -> None:
        roster = self.query_one(AgentList)
        roster.refresh_running()

        # Update other panes based on selection.
        selected = roster.selected_session_id
        self.query_one(DetailPanel).set_session(selected)
        self.query_one(TrajectoryView).set_session(selected)

    async def action_pause(self) -> None:
        self.services.control_plane.pause(self._selected_prefix())
        await self.action_refresh()

    async def action_resume(self) -> None:
        self.services.control_plane.resume(self._selected_prefix())
        await self.action_refresh()

    async def action_kill(self) -> None:
        self.services.control_plane.kill(self._selected_prefix())
        await self.action_refresh()

    async def action_directive(self) -> None:
        # Keep keybinding flow simple: send placeholder directive to selected.
        prefix = self._selected_prefix()
        self.services.control_plane.directive(prefix, "(placeholder directive)")

    async def action_launch(self) -> None:
        result = await self.push_screen_wait(
            LaunchAgentModal(
                working_dir=".",
                profile="readonly",
                model="gpt-5-mini",
                prompt="",
                auto_approve=False,
            )
        )
        if result is None:
            return
        self.services.launcher.launch(
            LaunchRequest(
                working_dir=result.working_dir,
                profile=result.profile,
                model=result.model,
                prompt=result.prompt,
                auto_approve=result.auto_approve,
            )
        )
        await self.action_refresh()

    async def action_command_palette(self) -> None:
        self.query_one(CommandInput).focus_input()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        # Only handle submissions from the palette input.
        if event.input.id != "command-input":
            return

        raw = event.value
        cmd_input = self.query_one(CommandInput)
        cmd_input.clear()

        try:
            parsed = parse_palette_command(raw, control_plane=self.services.control_plane)
        except ValueError as e:
            self.bell()
            self.notify(str(e), severity="error")
            return

        if parsed.name == "pause":
            self.services.control_plane.pause(parsed.target_prefix or "all")
            await self.action_refresh()
        elif parsed.name == "resume":
            self.services.control_plane.resume(parsed.target_prefix or "all")
            await self.action_refresh()
        elif parsed.name == "kill":
            self.services.control_plane.kill(parsed.target_prefix or "all")
            await self.action_refresh()
        elif parsed.name == "directive":
            assert parsed.target_prefix is not None
            self.services.control_plane.directive(parsed.target_prefix, parsed.text)
        elif parsed.name == "launch":
            await self.action_launch()
        elif parsed.name == "refresh":
            await self.action_refresh()
        else:
            self.notify(f"Unhandled command '{parsed.name}'", severity="error")


def run() -> None:
    """Entrypoint for manual runs (not wired into CLI yet)."""

    CommandCenterApp().run()
