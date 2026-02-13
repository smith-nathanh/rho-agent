"""Local signal-based transport implementation for command center controls."""

from __future__ import annotations

from datetime import datetime

from rho_agent.command_center.models import AgentStatus, RunningAgent
from rho_agent.signals import AgentInfo, SignalManager


class LocalSignalTransport:
    """Control transport adapter over the existing SignalManager protocol."""

    def __init__(self, signal_manager: SignalManager | None = None) -> None:
        self._signal_manager = signal_manager or SignalManager()

    def list_running(self) -> list[RunningAgent]:
        agents: list[RunningAgent] = []
        for info in self._signal_manager.list_running():
            agents.append(
                RunningAgent(
                    session_id=info.session_id,
                    pid=info.pid,
                    model=info.model,
                    instruction_preview=info.instruction_preview,
                    started_at=self._parse_started_at(info.started_at),
                    status=(
                        AgentStatus.PAUSED
                        if self._signal_manager.is_paused(info.session_id)
                        else AgentStatus.RUNNING
                    ),
                )
            )
        return agents

    def pause(self, session_id: str) -> bool:
        return self._signal_manager.pause(session_id)

    def resume(self, session_id: str) -> bool:
        return self._signal_manager.resume(session_id)

    def kill(self, session_id: str) -> bool:
        return self._signal_manager.cancel(session_id)

    def directive(self, session_id: str, text: str) -> bool:
        return self._signal_manager.queue_directive(session_id, text)

    def register_launcher_session(
        self,
        session_id: str,
        *,
        pid: int,
        model: str,
        instruction_preview: str,
    ) -> None:
        self._signal_manager.register(
            AgentInfo(
                session_id=session_id,
                pid=pid,
                model=model,
                instruction_preview=instruction_preview,
                started_at=datetime.now().astimezone().isoformat(),
            )
        )

    def deregister(self, session_id: str) -> None:
        self._signal_manager.deregister(session_id)

    @staticmethod
    def _parse_started_at(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
