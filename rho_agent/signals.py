"""File-based signal protocol for managing running agents.

Signal directory: ~/.config/rho-agent/signals/ (override via RHO_AGENT_SIGNAL_DIR)

Protocol:
- Agent starts -> writes <session_id>.running (JSON: pid, model, instruction preview, started_at)
- Agent ends -> deletes .running + .cancel files
- Kill command -> writes <session_id>.cancel
- Agent checks is_cancelled() -> stat() for .cancel file
"""

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


def _signal_dir() -> Path:
    """Get the signal directory, creating it if needed."""
    path = Path(
        os.getenv("RHO_AGENT_SIGNAL_DIR", str(Path.home() / ".config" / "rho-agent" / "signals"))
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class AgentInfo:
    """Information about a running agent, written to the .running file."""

    session_id: str
    pid: int
    model: str
    instruction_preview: str
    started_at: str  # ISO format

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "AgentInfo":
        return cls(**json.loads(data))


class SignalManager:
    """Manages file-based signals for agent lifecycle coordination."""

    def __init__(self, signal_dir: Path | None = None) -> None:
        self._dir = signal_dir or _signal_dir()

    def _running_path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.running"

    def _cancel_path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.cancel"

    def register(self, info: AgentInfo) -> None:
        """Write a .running file for this agent session."""
        self._running_path(info.session_id).write_text(info.to_json(), encoding="utf-8")

    def deregister(self, session_id: str) -> None:
        """Remove .running and .cancel files for this session."""
        for path in (self._running_path(session_id), self._cancel_path(session_id)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def is_cancelled(self, session_id: str) -> bool:
        """Check if a .cancel file exists (single stat() call)."""
        return self._cancel_path(session_id).exists()

    def cancel(self, session_id: str) -> bool:
        """Write a .cancel file for a specific session.

        Returns True if the session was found and cancel signal written.
        """
        if not self._running_path(session_id).exists():
            return False
        self._cancel_path(session_id).write_text("", encoding="utf-8")
        return True

    def cancel_by_prefix(self, prefix: str) -> list[str]:
        """Cancel all sessions whose ID starts with the given prefix.

        Returns list of cancelled session IDs.
        """
        cancelled = []
        for info in self.list_running():
            if info.session_id.startswith(prefix):
                self._cancel_path(info.session_id).write_text("", encoding="utf-8")
                cancelled.append(info.session_id)
        return cancelled

    def cancel_all(self) -> list[str]:
        """Cancel all running sessions.

        Returns list of cancelled session IDs.
        """
        cancelled = []
        for info in self.list_running():
            self._cancel_path(info.session_id).write_text("", encoding="utf-8")
            cancelled.append(info.session_id)
        return cancelled

    def list_running(self) -> list[AgentInfo]:
        """List all agents with .running files."""
        agents = []
        for path in self._dir.glob("*.running"):
            try:
                data = path.read_text(encoding="utf-8")
                agents.append(AgentInfo.from_json(data))
            except (json.JSONDecodeError, TypeError, KeyError):
                # Corrupt file, skip
                continue
        # Sort by started_at descending (most recent first)
        agents.sort(key=lambda a: a.started_at, reverse=True)
        return agents

    def cleanup_stale(self) -> list[str]:
        """Remove .running files for dead PIDs.

        Returns list of cleaned-up session IDs.
        """
        cleaned = []
        for info in self.list_running():
            if not _pid_alive(info.pid):
                self.deregister(info.session_id)
                cleaned.append(info.session_id)
        return cleaned


def _pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it
        return True
