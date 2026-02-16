"""Shared utilities for Daytona handlers."""


def shell_quote(s: str) -> str:
    """Quote a string for safe use in shell commands."""
    return "'" + s.replace("'", "'\\''") + "'"
