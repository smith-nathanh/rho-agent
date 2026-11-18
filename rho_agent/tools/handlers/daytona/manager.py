"""Sandbox lifecycle manager for Daytona cloud VMs (Daytona v2, SDK 2.x).

Shared by all Daytona handlers. Lazily creates a sandbox on first tool call
and tears it down on session close.
"""

from __future__ import annotations

import asyncio
import posixpath
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from .backend import DaytonaBackend

# Seconds to wait for an image pull or build, and for a sandbox to start.
_BUILD_TIMEOUT = 1800.0
_START_TIMEOUT = 300.0
# Daytona v2 always applies a wall-clock limit to a sandbox; a session's
# sandbox is deleted after this long even if close() never runs.
_SANDBOX_MAX_SECONDS = 24 * 3600


class SandboxManager:
    """Manages a single Daytona sandbox for the duration of a session."""

    _DEFAULT_ENV: ClassVar[dict[str, str]] = {
        "PATH": "/home/daytona/.local/bin:/usr/local/bin:/usr/bin:/bin",
    }

    def __init__(
        self,
        image: str | Any = "daytonaio/sandbox:latest",
        working_dir: str = "/home/daytona",
        env_vars: dict[str, str] | None = None,
        resources: Any = None,
        idle_timeout_minutes: int = 0,
        api_config: Any = None,
        uv_version: str | None = None,
    ):
        self._image = image
        self._uv_version = uv_version
        self._working_dir = working_dir
        self._env_vars = {**self._DEFAULT_ENV, **(env_vars or {})}
        self._resources = resources
        self._idle_timeout_minutes = idle_timeout_minutes
        self._api_config = api_config
        self._sandbox = None  # set once the sandbox is ready for tools
        self._starting = None  # a sandbox created but not yet ready
        self._client = None
        self._home: str | None = None
        self._lock = asyncio.Lock()

    @property
    def working_dir(self) -> str:
        return self._working_dir

    def resolve_path(self, path: str) -> str:
        """Resolve a tool path argument inside the sandbox.

        A relative path is taken from the working directory, the one bash runs
        in. Absolute paths and ``~`` paths are returned as given.
        """
        if path.startswith(("/", "~")):
            return path
        return posixpath.normpath(posixpath.join(self._working_dir, path))

    async def absolute_path(self, path: str) -> str:
        """Like :meth:`resolve_path`, with ``~`` expanded to the sandbox user's home.

        Daytona v2's file API takes absolute paths only, and a quoted ``~`` is
        not expanded by the shell either.
        """
        path = self.resolve_path(path)
        if path != "~" and not path.startswith("~/"):
            return path
        if self._home is None:
            from .ops import run

            sandbox = await self.get_sandbox()
            result = await run(sandbox, 'printf %s "$HOME"', timeout=30)
            self._home = result.output.strip() or "/"
        return posixpath.normpath(self._home + path[1:])

    @property
    def env_vars(self) -> dict[str, str]:
        return self._env_vars

    async def get_sandbox(self) -> Any:
        """Lazily create and return the sandbox (thread-safe)."""
        if self._sandbox is not None:
            return self._sandbox

        async with self._lock:
            # Double-check after acquiring lock
            if self._sandbox is not None:
                return self._sandbox

            from daytona import AsyncDaytona

            from .ops import prefer_image_git, run

            if self._api_config is not None:
                self._client = AsyncDaytona(self._api_config)
            else:
                self._client = AsyncDaytona()
            v2 = self._client.v2

            # Return as soon as the create is accepted and record the sandbox,
            # so close() deletes it even if this call is cancelled while it boots.
            self._starting = await v2.create(
                await self._resolve_image(v2),
                env=self._env_vars or None,
                resources=self._resources,
                lifecycle_policy=self._lifecycle_policy(),
                labels={"created-by": "rho-agent"},
                wait_seconds=0,
            )
            sandbox = self._starting
            try:
                await sandbox.wait(until="started", timeout=_START_TIMEOUT)
            except Exception:
                await self.close()
                raise
            await prefer_image_git(sandbox)

            # Ensure uv is available (an image may not ship it)
            check = await run(sandbox, "which uv 2>/dev/null", timeout=10)
            if check.exit_code != 0:
                uv_path = f"uv/{self._uv_version}" if self._uv_version else "uv"
                await run(
                    sandbox,
                    f"curl -LsSf https://astral.sh/{uv_path}/install.sh | sh",
                    env=self._env_vars,
                    timeout=60,
                )

            # Publish only now: get_sandbox() returns _sandbox without the lock.
            self._sandbox, self._starting = sandbox, None
            return sandbox

    async def _resolve_image(self, v2: Any) -> str:
        """Return the v2 image (name or id) to boot, pulling or building it if needed."""
        from .backend import DockerfileImage, RegisteredImage

        if isinstance(self._image, RegisteredImage):
            return self._image.name
        if isinstance(self._image, DockerfileImage):
            dockerfile = Path(self._image.path)
            submitted = await v2.images.build_from_context(
                str(dockerfile.parent), dockerfile=dockerfile.name
            )
            image = await v2.images.get(submitted.id, wait="ready", timeout=_BUILD_TIMEOUT)
        elif isinstance(self._image, str):
            image = await v2.images.pull(self._image, timeout=_BUILD_TIMEOUT)
        else:
            raise TypeError(
                "DaytonaBackend.image must be a registry reference (str), a DockerfileImage "
                f"or a RegisteredImage, got {type(self._image).__name__}"
            )
        status = getattr(image.status, "value", image.status)
        if status != "ready":
            raise RuntimeError(f"Daytona image {image.id} is not ready (status: {status})")
        return image.id

    def _lifecycle_policy(self) -> dict[str, Any]:
        """Wall-clock limit for the sandbox, plus the idle timeout when one is set."""
        from daytona.v2 import DISABLED

        policy: dict[str, Any] = {"timeout_seconds": _SANDBOX_MAX_SECONDS, "on_timeout": "delete"}
        if self._idle_timeout_minutes > 0:
            policy["idle_expiry_seconds"] = self._idle_timeout_minutes * 60
            policy["on_idle_timeout"] = "delete"
        else:
            # Explicitly off: an omitted idle timeout inherits the project default.
            policy["idle_expiry_seconds"] = DISABLED
        return policy

    async def close(self) -> None:
        """Delete the sandbox and close the client."""
        sandbox = self._sandbox or self._starting
        self._sandbox = self._starting = None
        self._home = None
        if sandbox is not None:
            try:
                await sandbox.delete()
            except Exception:
                pass  # Best-effort cleanup

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
        # Merge default PATH with user-supplied env vars (user wins)
        merged_env = {**cls._DEFAULT_ENV, **backend.env_vars}

        return cls(
            image=backend.image,
            working_dir=working_dir,
            env_vars=merged_env,
            resources=backend.resources,
            idle_timeout_minutes=backend.idle_timeout_minutes,
            api_config=backend.config,
            uv_version=backend.uv_version,
        )
