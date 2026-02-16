"""Sandbox lifecycle manager for Daytona cloud VMs.

Shared by all Daytona handlers. Lazily creates a sandbox on first tool call
and tears it down on session close.
"""

from __future__ import annotations

import asyncio
import os
from typing import Mapping


class SandboxManager:
    """Manages a single Daytona sandbox for the duration of a session."""

    def __init__(
        self,
        image: str = "ubuntu:latest",
        working_dir: str = "/home/daytona",
        env_vars: dict[str, str] | None = None,
        resources: dict[str, int] | None = None,
        auto_stop_interval: int = 0,
    ):
        self._image = image
        self._working_dir = working_dir
        self._env_vars = env_vars or {}
        self._resources = resources or {}
        self._auto_stop_interval = auto_stop_interval
        self._sandbox = None
        self._client = None
        self._lock = asyncio.Lock()

    @property
    def working_dir(self) -> str:
        return self._working_dir

    async def get_sandbox(self):
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
    def from_env(
        cls,
        working_dir: str = "/home/daytona",
        env: Mapping[str, str] | None = None,
    ) -> "SandboxManager":
        """Create a SandboxManager configured from environment variables."""
        resolved_env = env if env is not None else os.environ
        image = resolved_env.get("DAYTONA_SANDBOX_IMAGE", "ubuntu:latest")
        env_vars = {}
        resources = {}

        # Parse resource env vars
        if cpu := resolved_env.get("DAYTONA_SANDBOX_CPU"):
            resources["cpu"] = int(cpu)
        if memory := resolved_env.get("DAYTONA_SANDBOX_MEMORY"):
            resources["memory"] = int(memory)
        if disk := resolved_env.get("DAYTONA_SANDBOX_DISK"):
            resources["disk"] = int(disk)

        return cls(
            image=image,
            working_dir=working_dir,
            env_vars=env_vars,
            resources=resources if resources else None,
        )
