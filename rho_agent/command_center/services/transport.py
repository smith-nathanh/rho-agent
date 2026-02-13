"""Transport protocols for command center control operations."""

from __future__ import annotations

from typing import Protocol

from rho_agent.command_center.models import RunningAgent


class ControlTransport(Protocol):
    """Backend interface for controlling running agent sessions."""

    def list_running(self) -> list[RunningAgent]: ...

    def pause(self, session_id: str) -> bool: ...

    def resume(self, session_id: str) -> bool: ...

    def kill(self, session_id: str) -> bool: ...

    def directive(self, session_id: str, text: str) -> bool: ...

    def register_launcher_session(
        self,
        session_id: str,
        *,
        pid: int,
        model: str,
        instruction_preview: str,
    ) -> None: ...

    def deregister(self, session_id: str) -> None: ...
