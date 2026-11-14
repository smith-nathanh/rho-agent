"""Path helpers shared by the file tools: resolution and sandbox checks."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...sandbox import Sandbox

SENSITIVE_PATTERNS = [
    ".bashrc",
    ".zshrc",
    ".profile",
    ".bash_profile",
    ".ssh/",
    ".gnupg/",
    ".aws/",
    ".config/",
    "/etc/",
    "/usr/",
    "/bin/",
    "/sbin/",
]


def is_path_sensitive(path: str | Path) -> tuple[bool, str]:
    """Check if a path targets a sensitive location.

    Returns (is_sensitive, reason).
    """
    path_lower = str(Path(path).expanduser().resolve()).lower()
    for pattern in SENSITIVE_PATTERNS:
        if pattern in path_lower:
            return True, f"Cannot write to sensitive location: {path}"
    return False, ""


def resolve_path(path: str | Path, working_dir: str | Path | None = None) -> Path:
    """Resolve a tool path argument to an absolute path.

    A relative path is taken from ``working_dir`` (the agent's working
    directory, the one bash runs in), or from the process's current directory
    when there is none. Absolute paths and ``~`` are unaffected.
    """
    resolved = Path(path).expanduser()
    if working_dir and not resolved.is_absolute():
        resolved = Path(working_dir).expanduser() / resolved
    return resolved.resolve()


def sandbox_for(sandbox: Sandbox | None, invocation: Any) -> Sandbox | None:
    """``sandbox`` with the calling session's access grants applied."""
    if sandbox is None:
        return None
    grants = getattr(getattr(invocation, "session", None), "access_grants", None)
    return sandbox.with_grants(grants) if grants else sandbox


def read_refusal(sandbox: Sandbox | None, invocation: Any, path: str | Path) -> str | None:
    """Why the sandbox forbids reading ``path``, or None if it allows it."""
    effective = sandbox_for(sandbox, invocation)
    if effective is None:
        return None
    allowed, reason = effective.check_read(path)
    return None if allowed else reason


async def write_refusal(
    sandbox: Sandbox | None, invocation: Any, path: Path, action: str
) -> str | None:
    """Why the sandbox forbids writing ``path``, or None if it allows it.

    A path outside the writable directories is not refused outright when a
    user can answer: they are asked to allow its directory for the session.
    """
    effective = sandbox_for(sandbox, invocation)
    if effective is None:
        return None
    allowed, reason = effective.check_write(path)
    if allowed:
        return None
    session = getattr(invocation, "session", None)
    if session is None or session.approval_callback is None:
        return reason
    grant, _ = effective.policy.plan_grant(path.parent, write=True)
    if grant is None:
        return reason  # protected; not something to ask for
    approved = await session.ask_approval(
        "request_access",
        {"path": grant.path, "access": "write", "reason": f"{action} {path}"},
    )
    if not approved:
        return f"{reason}. The user declined write access to {grant.path}."
    session.grant_access(grant)
    return None
