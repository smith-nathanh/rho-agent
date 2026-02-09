"""File-based signal protocol for managing running agents.

Signal directory: ~/.config/rho-agent/signals/ (override via RHO_AGENT_SIGNAL_DIR)

Protocol:
- Agent starts -> writes <session_id>.running (JSON: pid, model, instruction preview, started_at)
- Agent ends -> deletes .running + .cancel files
- Kill command -> writes <session_id>.cancel
- Pause command -> writes <session_id>.pause
- Resume command -> deletes <session_id>.pause
- Directive command -> appends JSON lines to <session_id>.directive
- Agent checks is_cancelled() -> stat() for .cancel file
"""

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]


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

    def _pause_path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.pause"

    def _directive_path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.directive"

    def register(self, info: AgentInfo) -> None:
        """Write a .running file for this agent session."""
        self._running_path(info.session_id).write_text(info.to_json(), encoding="utf-8")

    def deregister(self, session_id: str) -> None:
        """Remove signal files for this session."""
        for path in (
            self._running_path(session_id),
            self._cancel_path(session_id),
            self._pause_path(session_id),
            self._directive_path(session_id),
        ):
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

    def is_paused(self, session_id: str) -> bool:
        """Check if a .pause file exists."""
        return self._pause_path(session_id).exists()

    def pause(self, session_id: str) -> bool:
        """Pause a specific session.

        Returns True if the session was found and pause signal written.
        """
        if not self._running_path(session_id).exists():
            return False
        self._pause_path(session_id).write_text("", encoding="utf-8")
        return True

    def resume(self, session_id: str) -> bool:
        """Resume a specific paused session.

        Returns True if the session exists (running or paused metadata).
        """
        running_exists = self._running_path(session_id).exists()
        pause_path = self._pause_path(session_id)
        if not running_exists and not pause_path.exists():
            return False
        try:
            pause_path.unlink()
        except FileNotFoundError:
            pass
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

    def pause_by_prefix(self, prefix: str) -> list[str]:
        """Pause all sessions whose ID starts with the given prefix."""
        paused = []
        for info in self.list_running():
            if info.session_id.startswith(prefix):
                self._pause_path(info.session_id).write_text("", encoding="utf-8")
                paused.append(info.session_id)
        return paused

    def resume_by_prefix(self, prefix: str) -> list[str]:
        """Resume all sessions whose ID starts with the given prefix."""
        resumed = []
        for info in self.list_running():
            if info.session_id.startswith(prefix):
                self.resume(info.session_id)
                resumed.append(info.session_id)
        return resumed

    def cancel_all(self) -> list[str]:
        """Cancel all running sessions.

        Returns list of cancelled session IDs.
        """
        cancelled = []
        for info in self.list_running():
            self._cancel_path(info.session_id).write_text("", encoding="utf-8")
            cancelled.append(info.session_id)
        return cancelled

    def queue_directive(self, session_id: str, directive: str) -> bool:
        """Queue an out-of-band directive for a running session."""
        if not self._running_path(session_id).exists():
            return False
        payload = {
            "directive": directive,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._directive_path(session_id).open("a+", encoding="utf-8") as f:
            self._lock_file(f)
            try:
                f.write(json.dumps(payload))
                f.write("\n")
                f.flush()
            finally:
                self._unlock_file(f)
        return True

    def consume_directives(self, session_id: str) -> list[str]:
        """Read and clear queued directives for a session."""
        path = self._directive_path(session_id)
        if not path.exists():
            return []

        try:
            with path.open("a+", encoding="utf-8") as f:
                self._lock_file(f)
                try:
                    f.seek(0)
                    lines = f.read().splitlines()
                    f.seek(0)
                    f.truncate(0)
                    f.flush()
                finally:
                    self._unlock_file(f)
        except OSError:
            return []

        directives: list[str] = []
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            directive = payload.get("directive")
            if isinstance(directive, str) and directive.strip():
                directives.append(directive.strip())
        return directives

    def _lock_file(self, file_obj: object) -> None:
        if fcntl is None:
            return
        fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX)  # type: ignore[union-attr]

    def _unlock_file(self, file_obj: object) -> None:
        if fcntl is None:
            return
        fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]

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
