"""Sandbox lifecycle manager for Daytona cloud VMs.

Shared by all Daytona handlers. Lazily creates a sandbox on first tool call
and tears it down on session close.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .backend import DaytonaBackend


class SandboxManager:
    """Manages a single Daytona sandbox for the duration of a session."""

    def __init__(
        self,
        image: str = "ubuntu:latest",
        working_dir: str = "/home/daytona",
        env_vars: dict[str, str] | None = None,
        resources: dict[str, int] | None = None,
        auto_stop_interval: int = 0,
        api_config: Any = None,
    ):
        self._image = image
        self._working_dir = working_dir
        self._env_vars = env_vars or {}
        self._resources = resources or {}
        self._auto_stop_interval = auto_stop_interval
        self._api_config = api_config
        self._sandbox = None
        self._client = None
        self._lock = asyncio.Lock()

    @property
    def working_dir(self) -> str:
        return self._working_dir

    async def get_sandbox(self) -> Any:
        """Lazily create and return the sandbox (thread-safe)."""
        if self._sandbox is not None:
            return self._sandbox

        async with self._lock:
            # Double-check after acquiring lock
            if self._sandbox is not None:
                return self._sandbox

            from daytona import (
                AsyncDaytona,
                CreateSandboxFromImageParams,
                Resources,
            )

            if self._api_config is not None:
                self._client = AsyncDaytona(self._api_config)
            else:
                self._client = AsyncDaytona()

            params = CreateSandboxFromImageParams(
                image=self._image,
                env_vars=self._env_vars if self._env_vars else None,
                auto_stop_interval=self._auto_stop_interval,
            )

            if self._resources:
                params.resources = Resources(
                    cpu=self._resources.get("cpu"),
                    memory=self._resources.get("memory"),
                    disk=self._resources.get("disk"),
                    gpu=self._resources.get("gpu"),
                )

            self._sandbox = await self._client.create(params, timeout=120)
            return self._sandbox

    async def close(self) -> None:
        """Delete the sandbox and close the client."""
        if self._sandbox is not None:
            try:
                await self._client.delete(self._sandbox, timeout=30)
            except Exception:
                pass  # Best-effort cleanup
            self._sandbox = None

        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    @classmethod
    def from_backend(
        cls,
        backend: DaytonaBackend,
        working_dir: str = "/home/daytona",
    ) -> SandboxManager:
        """Create a SandboxManager from a DaytonaBackend config."""
        resources: dict[str, int] = {}
        if backend.resources is not None:
            # Extract fields from the SDK Resources object
            for attr in ("cpu", "memory", "disk", "gpu"):
                val = getattr(backend.resources, attr, None)
                if val is not None:
                    resources[attr] = val

        return cls(
            image=backend.image,
            working_dir=working_dir,
            auto_stop_interval=backend.auto_stop_interval,
            resources=resources if resources else None,
            api_config=backend.config,
        )
