"""Shared path sensitivity checks for file-writing handlers."""

from __future__ import annotations

from pathlib import Path

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
