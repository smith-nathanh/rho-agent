"""Daytona cloud sandbox handlers."""

from __future__ import annotations

import os
from typing import Any

from .bash import DaytonaBashHandler
from .edit import DaytonaEditHandler
from .glob import DaytonaGlobHandler
from .grep import DaytonaGrepHandler
from .list import DaytonaListHandler
from .manager import SandboxManager
from .read import DaytonaReadHandler
from .write import DaytonaWriteHandler

__all__ = [
    "SandboxManager",
    "DaytonaBashHandler",
    "DaytonaReadHandler",
    "DaytonaWriteHandler",
    "DaytonaEditHandler",
    "DaytonaGlobHandler",
    "DaytonaGrepHandler",
    "DaytonaListHandler",
    "register_daytona_tools",
]


def register_daytona_tools(
    registry: "ToolRegistry",
    working_dir: str,
    env: dict[str, str] | None = None,
) -> SandboxManager:
    """Register Daytona remote handlers into a registry and return the SandboxManager.

    Raises:
        ImportError: If the ``daytona`` SDK is not installed.
    """
    from ...registry import ToolRegistry  # noqa: F811 — runtime import for type

    resolved_env = env if env is not None else dict(os.environ)
    manager = SandboxManager.from_env(working_dir=working_dir, env=resolved_env)

    registry.register(DaytonaBashHandler(manager))
    registry.register(DaytonaReadHandler(manager))
    registry.register(DaytonaWriteHandler(manager))
    registry.register(DaytonaEditHandler(manager))
    registry.register(DaytonaGlobHandler(manager))
    registry.register(DaytonaGrepHandler(manager))
    registry.register(DaytonaListHandler(manager))

    return manager
