"""meta.json — the registry record for one session directory.

The process that runs a session is the only writer of its ``meta.json``.
Monitors and ``ps`` only read it, so writes are atomic (write a temporary
file, then rename) and readers never see a half-written file.
"""

from __future__ import annotations

import getpass
import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

META_FILE = "meta.json"

# Statuses of a session whose process should still exist.
LIVE_STATUSES = frozenset({"created", "running", "waiting_input", "waiting_approval", "paused"})

# Shown instead of a live status when the owning process is gone.
DEAD = "dead"
# Shown instead of a live status when no owning process was recorded.
UNKNOWN = "unknown"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def process_identity() -> dict[str, Any]:
    """Who is running the session: process, machine and account."""
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    return {"pid": os.getpid(), "host": socket.gethostname(), "user": user}


def read_meta(session_dir: Path) -> dict[str, Any]:
    """Read ``meta.json``. Returns ``{}`` when it is missing or unreadable."""
    try:
        meta = json.loads((session_dir / META_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return meta if isinstance(meta, dict) else {}


def write_meta(session_dir: Path, meta: dict[str, Any]) -> None:
    """Replace ``meta.json`` atomically."""
    path = session_dir / META_FILE
    tmp = session_dir / f"{META_FILE}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def update_meta(
    session_dir: Path,
    *,
    defaults: dict[str, Any] | None = None,
    **fields: Any,
) -> None:
    """Merge fields into an existing ``meta.json``. Best-effort.

    Does nothing when the directory has no ``meta.json``: a session directory
    that was not created through ``SessionStore`` stays unregistered.

    Args:
        session_dir: The session directory.
        defaults: Fields written only when not already set.
        **fields: Fields to set.
    """
    try:
        if not (session_dir / META_FILE).exists():
            return
        meta = read_meta(session_dir)
        for key, value in (defaults or {}).items():
            if not meta.get(key):
                meta[key] = value
        meta.update(fields)
        write_meta(session_dir, meta)
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    """True when a process with this id exists on this machine."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another account
    except OSError:
        return False
    return True


def effective_status(meta: dict[str, Any]) -> str:
    """The recorded status, or ``dead`` when the owning process is gone.

    A killed process cannot write a final status, so a live status is checked
    against the recorded pid. The check is only possible for sessions started
    on this machine; for another host the recorded status is returned as is.
    A live status with no recorded pid (a session written before pids were
    recorded) cannot be checked at all and is reported as ``unknown``.
    """
    status = str(meta.get("status") or "completed")
    if status not in LIVE_STATUSES:
        return status
    pid = meta.get("pid")
    if not isinstance(pid, int):
        return UNKNOWN
    if meta.get("host") != socket.gethostname():
        return status
    return status if pid_alive(pid) else DEAD
