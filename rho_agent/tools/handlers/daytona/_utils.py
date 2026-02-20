"""Shared utilities for Daytona handlers."""

from __future__ import annotations


def shell_quote(s: str) -> str:
    """Quote a string for safe use in shell commands."""
    return "'" + s.replace("'", "'\\''") + "'"
