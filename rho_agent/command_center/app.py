"""Textual command-center app scaffold.

This is an early skeleton intended to support incremental migration from the legacy
prompt-toolkit monitor/dashboard.

The app wires the service layer (ControlPlane + LocalSignalTransport, TelemetryFeed,
AgentLauncher) and exposes placeholder keybindings that dispatch to control-plane
actions without crashing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Input

from rho_agent.command_center.commands import parse_palette_command
from rho_agent.command_center.modals.launch_agent_modal import LaunchAgentModal, LaunchModalResult
from rho_agent.command_center.models import LaunchRequest
from rho_agent.command_center.services.control_plane import ControlPlane
from rho_agent.command_center.services.launcher import AgentLauncher
from rho_agent.command_center.services.local_signal_transport import LocalSignalTransport
from rho_agent.command_center.services.telemetry_feed import TelemetryFeed
from rho_agent.observability.config import DEFAULT_TELEMETRY_DB
from rho_agent.observability.storage.sqlite import TelemetryStorage

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
        Binding("l", "launch", "Launch", priority=True),
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
            telemetry_feed = TelemetryFeed(TelemetryStorage(DEFAULT_TELEMETRY_DB))
        if launcher is None:
            launcher = AgentLauncher()

        self.services = CommandCenterServices(
            control_plane=control_plane,
            telemetry_feed=telemetry_feed,
            launcher=launcher,
        )

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            yield AgentList(id="roster")
            yield TrajectoryView(id="trajectory")
            yield DetailPanel(id="details")
        yield CommandInput(id="command")

        yield Footer()

    async def on_mount(self) -> None:
        # Wire services into widgets.
        roster = self.query_one(AgentList)
        roster.services = self.services
        self.query_one(TrajectoryView).services = self.services
        self.query_one(DetailPanel).services = self.services
        self.query_one(CommandInput).services = self.services

        # Initial state.
        await self.action_refresh()
        self.set_interval(0.75, self._background_refresh_tick)
        if roster.selected_session_id is None:
            command = self.query_one(CommandInput)
            command.scroll_visible()
            command.focus_input()
        else:
            roster.focus()

    def _selected_prefix(self) -> str:
        roster = self.query_one(AgentList)
        return roster.selected_session_prefix or "all"

    async def action_roster_down(self) -> None:
        self.query_one(AgentList).move_selection(1)
        self._sync_selected_panes()

    async def action_roster_up(self) -> None:
        self.query_one(AgentList).move_selection(-1)
        self._sync_selected_panes()

    async def action_refresh(self) -> None:
        roster = self.query_one(AgentList)
        roster.refresh_running()
        self._sync_selected_panes()

    def _sync_selected_panes(self) -> None:
        selected = self.query_one(AgentList).selected_session_id
        self.query_one(DetailPanel).set_session(selected)
        self.query_one(TrajectoryView).set_session(selected)

    def _background_refresh_tick(self) -> None:
        # Poll running sessions in the background to keep roster live.
        self.run_worker(self.action_refresh(), exclusive=True, group="refresh")

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
        await self.push_screen(
            LaunchAgentModal(
                working_dir=".",
                profile="readonly",
                model="gpt-5-mini",
                prompt="",
                auto_approve=False,
                team_id=os.getenv("RHO_AGENT_TEAM_ID", ""),
                project_id=os.getenv("RHO_AGENT_PROJECT_ID", ""),
            ),
            callback=self._on_launch_modal_closed,
        )

    def _on_launch_modal_closed(self, result: LaunchModalResult | None) -> None:
        if result is None:
            return
        try:
            launched = self.services.launcher.launch(
                LaunchRequest(
                    working_dir=result.working_dir,
                    profile=result.profile,
                    model=result.model,
                    prompt=result.prompt,
                    auto_approve=result.auto_approve,
                    team_id=result.team_id or None,
                    project_id=result.project_id or None,
                )
            )
        except Exception as exc:
            self.notify(str(exc), severity="error")
            return
        if launched is not None:
            self.notify(
                f"Launched {launched.session_id} (pid {launched.pid})",
                severity="information",
            )
        self.run_worker(self.action_refresh())

    async def action_command_palette(self) -> None:
        command = self.query_one(CommandInput)
        command.scroll_visible()
        command.focus_input()

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
