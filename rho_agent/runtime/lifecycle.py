"""Runtime session lifecycle helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .protocol import Runtime


async def start_runtime(runtime: Runtime) -> None:
    """Start runtime-level telemetry session if configured."""
    await runtime.start()


async def close_runtime(runtime: Runtime, status: str = "completed") -> None:
    """Close runtime-level telemetry session if configured."""
    await runtime.close(status)
