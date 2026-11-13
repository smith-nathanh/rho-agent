"""SessionStore — directory manager for sessions.

Creates session directories, discovers sessions, enables resume.
Does not own the data — State writes trace.jsonl incrementally.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .agent import Agent
from .approvals import ApprovalRequest, pending_requests
from .config import AgentConfig
from .session import Session
from .session_meta import (
    LIVE_STATUSES,
    effective_status,
    now_iso,
    process_identity,
    read_meta,
    update_meta,
    write_meta,
)
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
    # Project the session works in (its working directory).
    cwd: str = ""
    user: str = ""
    host: str = ""
    pid: int | None = None
    cost_usd: float = 0.0
    last_activity: str = ""
    parent_id: str = ""
    path: Path | None = None
    # Sessions spawned by this one (delegate and agent tools), oldest first.
    children: list[SessionInfo] = field(default_factory=list)

    @property
    def live(self) -> bool:
        """True while the session's process is expected to be running."""
        return self.status in LIVE_STATUSES

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
            meta.json       # registry record: status, owner, project, usage
            cancel          # sentinel file (touch to cancel the current run)
            children/<id>/  # sessions spawned by this one, same layout

    ``meta.json`` is written by the process running the session; see
    ``session_meta`` for its fields and how a dead process is detected.
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        """Open the store at ``base_dir``, by default the CLI's own
        ``~/.config/rho-agent/sessions``, so `rho-agent monitor` sees the sessions."""
        if base_dir is None:
            base_dir = Path.home() / ".config" / "rho-agent" / "sessions"
        self._base_dir = Path(base_dir).expanduser()
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

        if session_id:
            sid = session_id
            session_dir = self._base_dir / sid
            session_dir.mkdir(parents=True, exist_ok=True)
        else:
            # A generated ID must never land on an existing session's directory.
            while True:
                sid = uuid.uuid4().hex[:8]
                session_dir = self._base_dir / sid
                try:
                    session_dir.mkdir(parents=True)
                    break
                except FileExistsError:
                    continue

        # Write config
        agent.config.to_file(session_dir / "config.yaml")

        # Set up state with trace file
        trace_path = session_dir / "trace.jsonl"
        state = State(trace_path=trace_path)

        started = now_iso()
        write_meta(
            session_dir,
            {
                "id": sid,
                "status": "created",
                "model": agent.config.model,
                "profile": agent.config.profile,
                "cwd": agent.config.working_dir or os.getcwd(),
                **process_identity(),
                "started_at": started,
                "last_activity": started,
            },
        )

        return Session(agent, session_id=sid, state=state, session_dir=session_dir)

    def update_status(self, session_id: str, status: str) -> None:
        """Update the status field in a session's meta.json."""
        update_meta(self._base_dir / session_id, status=status)

    def find(self, prefix: str) -> list[SessionInfo]:
        """Sessions whose ID starts with ``prefix``, child sessions included.

        ``all`` matches every top-level session; their children follow them.
        """
        matches = []
        for d in self._base_dir.iterdir():
            if not d.is_dir():
                continue
            info = self._read_session_info(d)
            if info is None:
                continue
            if prefix == "all":
                matches.append(info)
            else:
                matches.extend(s for s in _walk(info) if s.id.startswith(prefix))
        return matches

    def pending_approvals(self) -> list[tuple[SessionInfo, ApprovalRequest]]:
        """Approval requests that live sessions are waiting on, oldest first."""
        pending = []
        for top in self.find("all"):
            for info in _walk(top):
                if info.live and info.path is not None:
                    pending.extend((info, request) for request in pending_requests(info.path))
        pending.sort(key=lambda pair: pair[1].requested_at)
        return pending

    def request_cancel(self, info: SessionInfo) -> bool:
        """Ask a live session to stop its current run.

        Returns False without doing anything when the session is not live: a
        cancel request left on a finished session would stop its next run.
        """
        if not info.live or info.path is None:
            return False
        (info.path / "cancel").touch()
        return True

    def load_config(self, session_id: str) -> AgentConfig:
        """The config a session was saved with (defaults if it has none)."""
        config_path = self._resolve_session_dir(session_id) / "config.yaml"
        if config_path.exists():
            return AgentConfig.from_file(config_path)
        return AgentConfig()

    def resume(
        self,
        session_id: str,
        *,
        agent_config: AgentConfig | None = None,
    ) -> Session:
        """Resume a session by loading config and replaying trace.

        Args:
            session_id: The session ID (directory name) to resume.
            agent_config: Optional config that replaces the saved one entirely.
                To change a few fields, start from ``load_config()``.

        Returns:
            A Session with restored state.
        """
        session_dir = self._resolve_session_dir(session_id)
        # A cancel request that was never consumed must not stop the resumed run.
        (session_dir / "cancel").unlink(missing_ok=True)

        agent = Agent(agent_config or self.load_config(session_id))

        # Replay trace to restore state
        trace_path = session_dir / "trace.jsonl"
        if trace_path.exists():
            state = State.from_jsonl(trace_path.read_bytes())
        else:
            state = State()
        state.trace_path = trace_path

        return Session(agent, session_id=session_id, state=state, session_dir=session_dir)

    def list(self, limit: int = 20) -> list[SessionInfo]:
        """List sessions, most recently active first."""
        results = []
        for d in self._base_dir.iterdir():
            if not d.is_dir():
                continue
            info = self._read_session_info(d)
            if info:
                results.append(info)
        results.sort(key=lambda info: info.last_activity, reverse=True)
        return results[:limit]

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
        """Read session info, including child sessions, from a session directory."""
        try:
            meta = read_meta(session_dir)
            trace_path = session_dir / "trace.jsonl"
            activity_path = trace_path if trace_path.exists() else session_dir
            modified = datetime.fromtimestamp(activity_path.stat().st_mtime, tz=UTC).isoformat()

            model = meta.get("model", "")
            profile = meta.get("profile", "")
            cwd = meta.get("cwd", "")
            config_path = session_dir / "config.yaml"
            if config_path.exists() and not (model and profile and cwd):
                try:
                    config = AgentConfig.from_file(config_path)
                    model = model or config.model
                    profile = profile or config.profile
                    cwd = cwd or (config.working_dir or "")
                except Exception:
                    pass

            children = []
            children_dir = session_dir / "children"
            if children_dir.is_dir():
                for child_dir in children_dir.iterdir():
                    child = self._read_session_info(child_dir) if child_dir.is_dir() else None
                    if child:
                        children.append(child)
                children.sort(key=lambda info: info.created_at)

            pid = meta.get("pid")
            return SessionInfo(
                id=session_dir.name,
                status=effective_status(meta),
                created_at=meta.get("started_at") or modified,
                model=model,
                profile=profile,
                first_prompt=meta.get("first_prompt") or _first_prompt_from_trace(trace_path),
                cwd=cwd,
                user=meta.get("user", ""),
                host=meta.get("host", ""),
                pid=pid if isinstance(pid, int) else None,
                cost_usd=float(meta.get("cost_usd") or 0.0),
                last_activity=max(meta.get("last_activity") or "", modified),
                parent_id=meta.get("parent_session_id", ""),
                path=session_dir,
                children=children,
            )
        except Exception:
            return None


def _walk(info: SessionInfo):
    """A session followed by all of its descendants."""
    yield info
    for child in info.children:
        yield from _walk(child)


def _first_prompt_from_trace(trace_path: Path) -> str:
    """The first prompt of a session whose meta.json does not record it."""
    try:
        with open(trace_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("type") == "run_start":
                    return str(event.get("prompt", ""))
                if event.get("type") == "message" and event.get("role") == "user":
                    content = event.get("content", "")
                    return content if isinstance(content, str) else ""
    except Exception:
        pass
    return ""
