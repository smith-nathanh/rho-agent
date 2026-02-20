"""Transport protocols for agent control operations."""

from __future__ import annotations

from typing import Protocol

from rho_agent.control.models import RunningAgent


class ControlTransport(Protocol):
    """Backend interface for controlling running agent sessions."""

    def list_running(self) -> list[RunningAgent]:
        """Return all currently running agents."""
        ...

    def pause(self, session_id: str) -> bool:
        """Pause the agent with the given session ID."""
        ...

    def resume(self, session_id: str) -> bool:
        """Resume the agent with the given session ID."""
        ...

    def kill(self, session_id: str) -> bool:
        """Kill the agent with the given session ID."""
        ...

    def directive(self, session_id: str, text: str) -> bool:
        """Queue a directive for the agent with the given session ID."""
        ...

    def register_launcher_session(
        self,
        session_id: str,
        *,
        pid: int,
        model: str,
        instruction_preview: str,
    ) -> None:
        """Register a newly launched agent session."""
        ...

    def deregister(self, session_id: str) -> None:
        """Remove a session from tracking."""
        ...
