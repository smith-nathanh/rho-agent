"""Shared path sensitivity checks for file-writing handlers."""

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
