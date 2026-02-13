"""UI-agnostic control-plane orchestration for command center actions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from rho_agent.command_center.models import RunningAgent
from rho_agent.command_center.services.transport import ControlTransport


@dataclass(slots=True)
class ControlOutcome:
    """Result of applying a control-plane operation."""

    acted_session_ids: list[str] = field(default_factory=list)
    error: str | None = None
    warning: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.acted_session_ids)


class ControlPlane:
    """Resolve session prefixes and dispatch control actions via transport."""

    def __init__(self, transport: ControlTransport) -> None:
        self._transport = transport

    def list_running(self) -> list[RunningAgent]:
        return self._transport.list_running()

    def resolve_running_prefix(self, prefix: str) -> list[str]:
        agents = self._transport.list_running()
        if prefix == "all":
            return [agent.session_id for agent in agents]
        return [agent.session_id for agent in agents if agent.session_id.startswith(prefix)]

    def resolve_single_running(self, prefix: str) -> tuple[str | None, str | None]:
        matches = self.resolve_running_prefix(prefix)
        if not matches:
            return None, f"No running agents matching '{prefix}'"
        if len(matches) > 1:
            return None, f"Prefix '{prefix}' matched multiple sessions; use a longer prefix."
        return matches[0], None

    def kill(self, prefix: str) -> ControlOutcome:
        return self._apply_many(prefix, self._transport.kill)

    def pause(self, prefix: str) -> ControlOutcome:
        return self._apply_many(prefix, self._transport.pause)

    def resume(self, prefix: str) -> ControlOutcome:
        return self._apply_many(prefix, self._transport.resume)

    def directive(self, prefix: str, text: str) -> ControlOutcome:
        session_id, error = self.resolve_single_running(prefix)
        if error:
            if "multiple sessions" in error:
                return ControlOutcome(warning=error)
            return ControlOutcome(error=error)
        if session_id is None:
            return ControlOutcome(error=f"No running agents matching '{prefix}'")
        if not self._transport.directive(session_id, text):
            return ControlOutcome(error=f"Failed to queue directive for {session_id[:8]}")
        return ControlOutcome(acted_session_ids=[session_id])

    def _apply_many(
        self,
        prefix: str,
        op: Callable[[str], bool],
    ) -> ControlOutcome:
        targets = self.resolve_running_prefix(prefix)
        if not targets:
            return ControlOutcome(error=f"No running agents matching '{prefix}'")
        acted = [session_id for session_id in targets if op(session_id)]
        if acted:
            return ControlOutcome(acted_session_ids=acted)
        return ControlOutcome(warning="No agents updated")
