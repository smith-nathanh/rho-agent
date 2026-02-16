"""Runtime session lifecycle helpers."""

from __future__ import annotations

from .types import AgentRuntime


async def start_runtime(runtime: AgentRuntime) -> None:
    """Start runtime-level telemetry session if configured."""
    if runtime.observability:
        await runtime.observability.start_session()


async def close_runtime(runtime: AgentRuntime, status: str = "completed") -> None:
    """Close runtime-level telemetry session and sandbox if configured."""
    # Clean up Daytona sandbox if one was created
    manager = getattr(runtime.registry, "_sandbox_manager", None)
    if manager is not None:
        await manager.close()

    if runtime.observability:
        await runtime.observability.end_session(status)
