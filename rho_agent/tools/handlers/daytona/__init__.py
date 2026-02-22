"""Daytona cloud sandbox handlers."""

from __future__ import annotations

from typing import Any

from .backend import DaytonaBackend
from .bash import DaytonaBashHandler
from .edit import DaytonaEditHandler
from .glob import DaytonaGlobHandler
from .grep import DaytonaGrepHandler
from .list import DaytonaListHandler
from .manager import SandboxManager
from .read import DaytonaReadHandler
from .write import DaytonaWriteHandler

__all__ = [
    "DaytonaBackend",
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
    backend: DaytonaBackend | None = None,
) -> SandboxManager:
    """Register Daytona remote handlers into a registry and return the SandboxManager.

    Raises:
        ImportError: If the ``daytona`` SDK is not installed.
    """
    if backend is not None:
        manager = SandboxManager.from_backend(backend, working_dir=working_dir)
    else:
        manager = SandboxManager(working_dir=working_dir)

    registry.register(DaytonaBashHandler(manager))
    registry.register(DaytonaReadHandler(manager))
    registry.register(DaytonaWriteHandler(manager))
    registry.register(DaytonaEditHandler(manager))
    registry.register(DaytonaGlobHandler(manager))
    registry.register(DaytonaGrepHandler(manager))
    registry.register(DaytonaListHandler(manager))

    return manager
