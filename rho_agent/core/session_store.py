"""SessionStore — directory manager for sessions.

Creates session directories, discovers sessions, enables resume.
Does not own the data — State writes trace.jsonl incrementally.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent import Agent
from .config import AgentConfig
from .session import Session
from .state import State


@dataclass
class SessionInfo:
    """Metadata for a discovered session."""

    id: str
    status: str
    created_at: str
    model: str = ""
    profile: str = ""
    first_prompt: str = ""

    @property
    def display_preview(self) -> str:
        preview = self.first_prompt[:60]
        if len(self.first_prompt) > 60:
            preview += "..."
        return preview


class SessionStore:
    """Directory manager for session persistence, discovery, and resume.

    Manages a base directory where each session gets its own subdirectory.
    Does NOT own session data — State writes ``trace.jsonl`` incrementally,
    SessionStore just manages the directory layout and provides create/resume/list.

    Directory layout per session::

        <base_dir>/<session_id>/
            config.yaml     # AgentConfig (for resume)
            trace.jsonl     # append-only event log (State writes this)
            meta.json       # pid, model, status (programmatic sessions only)
            cancel          # sentinel file (touch to request cancellation)

    CLI sessions (under ``~/.config/rho-agent/sessions/``) are lightweight —
    just ``config.yaml`` + ``trace.jsonl``, no ``meta.json`` or control plane files.
    """

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def create_session(
        self,
        agent: Agent,
        *,
        session_id: str | None = None,
    ) -> Session:
        """Create a new session with a directory, writing config.yaml and meta.json.

        Args:
            agent: The agent to create a session for.
            session_id: Optional custom session ID.

        Returns:
            A Session with trace_path set for incremental writing.
        """
        import uuid

        sid = session_id or str(uuid.uuid4())[:8]
        session_dir = self._base_dir / sid
        session_dir.mkdir(parents=True, exist_ok=True)

        # Write config
        agent.config.to_file(session_dir / "config.yaml")

        # Set up state with trace file
        trace_path = session_dir / "trace.jsonl"
        state = State(trace_path=trace_path)

        # Always write meta.json
        meta = {
            "model": agent.config.model,
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        (session_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

        return Session(agent, session_id=sid, state=state, session_dir=session_dir)

    def update_status(self, session_id: str, status: str) -> None:
        """Update the status field in a session's meta.json."""
        session_dir = self._base_dir / session_id
        meta_path = session_dir / "meta.json"
        if not meta_path.exists():
            return
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["status"] = status
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def resume(
        self,
        session_id: str,
        *,
        agent_config: AgentConfig | None = None,
    ) -> Session:
        """Resume a session by loading config and replaying trace.

        Args:
            session_id: The session ID (directory name) to resume.
            agent_config: Optional override config. If None, loads from session dir.

        Returns:
            A Session with restored state.
        """
        session_dir = self._resolve_session_dir(session_id)

        # Load config
        config_path = session_dir / "config.yaml"
        if agent_config is None:
            if config_path.exists():
                agent_config = AgentConfig.from_file(config_path)
            else:
                agent_config = AgentConfig()

        # Replay trace to restore state
        trace_path = session_dir / "trace.jsonl"
        if trace_path.exists():
            state = State.from_jsonl(trace_path.read_bytes())
        else:
            state = State()
        state.trace_path = trace_path

        agent = Agent(agent_config)
        return Session(agent, session_id=session_id, state=state, session_dir=session_dir)

    def list(self, limit: int = 20) -> list[SessionInfo]:
        """List sessions, newest first."""
        dirs = sorted(
            [d for d in self._base_dir.iterdir() if d.is_dir()],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )[:limit]

        results = []
        for d in dirs:
            info = self._read_session_info(d)
            if info:
                results.append(info)
        return results

    def get_latest_id(self) -> str | None:
        """Get the ID of the most recent session."""
        sessions = self.list(limit=1)
        return sessions[0].id if sessions else None

    def _resolve_session_dir(self, session_id: str) -> Path:
        """Resolve a session ID to its directory path."""
        # Direct path
        direct = Path(session_id)
        if direct.is_dir():
            return direct
        # Relative to base_dir
        relative = self._base_dir / session_id
        if relative.is_dir():
            return relative
        raise FileNotFoundError(f"Session not found: {session_id}")

    def _read_session_info(self, session_dir: Path) -> SessionInfo | None:
        """Read session info from a session directory."""
        try:
            sid = session_dir.name
            status = "completed"
            created_at = datetime.fromtimestamp(
                session_dir.stat().st_mtime, tz=timezone.utc
            ).isoformat()
            model = ""
            profile = ""
            first_prompt = ""

            # Try meta.json first
            meta_path = session_dir / "meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                status = meta.get("status", "completed")
                created_at = meta.get("started_at", created_at)
                model = meta.get("model", "")

            # Try config.yaml
            config_path = session_dir / "config.yaml"
            if config_path.exists():
                try:
                    config = AgentConfig.from_file(config_path)
                    model = model or config.model
                    profile = config.profile
                except Exception:
                    pass

            # Try trace.jsonl for first prompt
            trace_path = session_dir / "trace.jsonl"
            if trace_path.exists():
                try:
                    with open(trace_path, encoding="utf-8") as f:
                        for line in f:
                            if not line.strip():
                                continue
                            event = json.loads(line)
                            if event.get("event") == "run_start":
                                first_prompt = event.get("prompt", "")
                                break
                            if event.get("event") == "message" and event.get("role") == "user":
                                first_prompt = event.get("content", "")
                                break
                except Exception:
                    pass

            return SessionInfo(
                id=sid,
                status=status,
                created_at=created_at,
                model=model,
                profile=profile,
                first_prompt=first_prompt,
            )
        except Exception:
            return None
