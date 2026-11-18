"""Harbor environment for Daytona v2 (api.daytona.com), through Daytona SDK 2.x.

Harbor's own ``daytona`` environment uses SDK 0.x and the Daytona v0 platform,
whose sandboxes are capped at 4 vCPU / 8 GiB / 10 GiB. Daytona v2 allows up to
16 cores and 32 GiB per sandbox. With SDK 2.x installed, Harbor's built-in
Daytona environment is unavailable, so jobs select this one by import path::

    environment:
      import_path: rho_agent.eval.harbor.daytona_v2:DaytonaV2Environment

or ``harbor run -e rho_agent.eval.harbor.daytona_v2:DaytonaV2Environment``.

Supported: single-container tasks built from the task's Dockerfile or pulled
from its ``docker_image``; the task's CPU, memory and disk; separate verifier
environments; public, no-network and allow-list network policies, including
switching policy between the agent and verifier phases. Not supported:
multi-container (docker-compose) tasks, GPUs, Windows.

Auth: ``DAYTONA_API_KEY`` (a key that can write sandboxes and images on
Daytona v2). SDK 2.x ignores ``DAYTONA_API_URL``.
"""

from __future__ import annotations

import asyncio
import math
import shlex
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities
from harbor.models.task.config import (
    NetworkAllowlistEntryType,
    NetworkMode,
    NetworkPolicy,
    classify_network_allowlist_entry,
)

from rho_agent.tools.handlers.daytona.ops import exit_code_of, prefer_image_git

ENVIRONMENT_TYPE = "daytona-v2"

# Daytona v2 always applies a wall-clock limit to a Linux sandbox and deletes it
# when it passes. Set it well past any task's agent and verifier time limits.
_SANDBOX_MAX_SECONDS = 24 * 3600
_CREATE_WAIT_SECONDS = 180
_POLICY_APPLY_SECONDS = 120.0


def network_policy_input(policy: NetworkPolicy) -> dict[str, Any] | None:
    """Translate a Harbor network policy to a Daytona v2 ``network_policy``.

    Public is ``None`` (no policy); no-network blocks everything; an allow list
    permits only the listed hostnames and IPv4 addresses or ranges.
    """
    if policy.network_mode == NetworkMode.PUBLIC:
        return None
    if policy.network_mode == NetworkMode.NO_NETWORK:
        return {"block_all": True}
    domains: list[str] = []
    cidrs: list[str] = []
    for host in policy.allowed_hosts:
        kind = classify_network_allowlist_entry(host)
        if kind == NetworkAllowlistEntryType.HOSTNAME:
            domains.append(host)
        elif kind == NetworkAllowlistEntryType.IPV4_ADDRESS:
            cidrs.append(f"{host}/32")
        elif kind == NetworkAllowlistEntryType.IPV4_CIDR:
            cidrs.append(host)
        else:
            raise ValueError(f"Daytona v2 environment cannot allow {host!r} ({kind.value})")
    return {"block_all": True, "allow_domains": domains, "allow_cidrs": cidrs}


class DaytonaV2Environment(BaseEnvironment):
    @staticmethod
    def type() -> str:
        return ENVIRONMENT_TYPE

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(
            disable_internet=True,
            network_allowlist=True,
            network_allowlist_hostnames=True,
            network_allowlist_ipv4_addresses=True,
            network_allowlist_ipv4_cidrs=True,
            dynamic_network_policy=True,
        )

    def _validate_definition(self) -> None:
        if self.task_env_config.docker_image:
            return
        dockerfile = Path(self.environment_dir) / "Dockerfile"
        if not dockerfile.exists():
            raise FileNotFoundError(
                f"{dockerfile} not found; the Daytona v2 environment needs a Dockerfile "
                "or docker_image (docker-compose tasks are not supported)"
            )

    # --- lifecycle ---------------------------------------------------------

    async def start(self, force_build: bool) -> None:
        from daytona import AsyncDaytona
        from daytona.v2 import Resources

        self._client = AsyncDaytona()
        v2 = self._client.v2

        image_id = await self._resolve_image(v2)
        cpus = self._effective_cpus or 1
        memory_gib = max(1, math.ceil((self._effective_memory_mb or 2048) / 1024))
        disk_gib = max(1, math.ceil((self._effective_storage_mb or 10240) / 1024))
        create_kwargs: dict[str, Any] = {
            "resources": Resources(cpu=cpus, memory=memory_gib, disk=disk_gib),
            "labels": {"created-by": "rho-agent-harbor", "harbor-session": self.session_id[:63]},
            "lifecycle_policy": {"timeout_seconds": _SANDBOX_MAX_SECONDS, "on_timeout": "delete"},
            "wait_seconds": _CREATE_WAIT_SECONDS,
        }
        policy = network_policy_input(self.network_policy)
        if policy is not None:
            create_kwargs["network_policy"] = policy
        self._sandbox = await v2.create(image_id, **create_kwargs)
        state = getattr(self._sandbox.state, "value", self._sandbox.state)
        if state != "started":
            sandbox_id = self._sandbox.id
            await self._delete_sandbox()
            raise RuntimeError(f"Daytona v2 sandbox {sandbox_id} did not start (state: {state})")
        self.logger.info(
            "Daytona v2 sandbox %s started: %s vCPU, %s GiB memory, %s GiB disk, network %s",
            self._sandbox.id,
            cpus,
            memory_gib,
            disk_gib,
            self.network_policy.network_mode.value,
        )
        if await prefer_image_git(self._sandbox):
            self.logger.info("Using the image's git; Daytona's moved to /usr/local/bin/daytona-git")

        workdir = self.task_env_config.workdir
        if workdir:
            await self.exec(self._ensure_dirs_command([workdir], chmod=False), user="root")
        await self.ensure_dirs(self._mount_targets(writable_only=True))
        await self._upload_environment_dir_after_start()

    async def _resolve_image(self, v2: Any) -> str:
        timeout = float(self.task_env_config.build_timeout_sec or 1800)
        if self.task_env_config.docker_image:
            image = await v2.images.pull(self.task_env_config.docker_image, timeout=timeout)
        else:
            submitted = await v2.images.build_from_context(str(self.environment_dir))
            image = await v2.images.get(submitted.id, wait="ready", timeout=timeout)
        status = getattr(image.status, "value", image.status)
        if status != "ready":
            raise RuntimeError(f"Daytona v2 image {image.id} is not ready (status: {status})")
        self.logger.info("Daytona v2 image %s ready", image.id)
        return image.id

    async def stop(self, delete: bool) -> None:
        if delete:
            await self._delete_sandbox()
        client = getattr(self, "_client", None)
        if client is not None:
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass
            self._client = None

    async def _delete_sandbox(self) -> None:
        sandbox = getattr(self, "_sandbox", None)
        if sandbox is None:
            return
        try:
            await sandbox.delete()
            self.logger.info("Daytona v2 sandbox %s deleted", sandbox.id)
        except Exception as e:  # Best effort; v2's wall-clock limit deletes it later.
            self.logger.warning("Could not delete Daytona v2 sandbox %s: %s", sandbox.id, e)
        self._sandbox = None

    # --- network policy ----------------------------------------------------

    async def _apply_network_policy(self, network_policy: NetworkPolicy) -> None:
        sandbox = self._require_sandbox()
        info = await sandbox.get_network_policy()
        policy = network_policy_input(network_policy)
        if policy is None:
            info = await sandbox.reset_network_policy(if_match=info.etag)
        else:
            info = await sandbox.update_network_policy(network_policy=policy, if_match=info.etag)
        await self._wait_for_policy(info)

    async def _wait_for_policy(self, info: Any) -> None:
        """Wait until the sandbox reports the new policy as applied."""
        sandbox = self._require_sandbox()
        deadline = time.monotonic() + _POLICY_APPLY_SECONDS
        while True:
            if info.error:
                raise RuntimeError(f"Daytona v2 network policy failed: {info.error}")
            if info.applied_revision is not None and info.applied_revision >= info.desired_revision:
                return
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"Daytona v2 network policy revision {info.desired_revision} was not "
                    f"applied within {_POLICY_APPLY_SECONDS:.0f}s (status: {info.status})"
                )
            await asyncio.sleep(1.0)
            info = await sandbox.get_network_policy()

    # --- commands ----------------------------------------------------------

    def _require_sandbox(self) -> Any:
        sandbox = getattr(self, "_sandbox", None)
        if sandbox is None:
            raise RuntimeError("Daytona v2 sandbox is not running; call start() first")
        return sandbox

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        sandbox = self._require_sandbox()
        user = self._resolve_user(user)
        env = self._merge_env(env)
        result = await sandbox.process.run(
            command,
            cwd=cwd,
            env=env or None,
            user=str(user) if user is not None else None,
            execution_timeout=float(timeout_sec) if timeout_sec else None,
            timeout=float(timeout_sec) + 60 if timeout_sec else None,
        )
        return ExecResult(
            stdout=result.stdout, stderr=result.stderr, return_code=exit_code_of(result)
        )

    # --- files -------------------------------------------------------------

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        sandbox = self._require_sandbox()
        parent = str(Path(target_path).parent)
        await self._checked_exec(f"mkdir -p {shlex.quote(parent)}")
        await sandbox.fs.upload_file(str(source_path), target_path)

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        sandbox = self._require_sandbox()
        remote_tar = f"/tmp/harbor-upload-{uuid.uuid4().hex}.tar.gz"
        with tempfile.TemporaryDirectory() as tmp:
            local_tar = Path(tmp) / "upload.tar.gz"
            with tarfile.open(local_tar, mode="w:gz") as tar:
                tar.add(str(source_dir), arcname=".")
            await sandbox.fs.upload_file(str(local_tar), remote_tar)
        target = shlex.quote(target_dir)
        await self._checked_exec(
            f"mkdir -p {target} && tar -xzf {remote_tar} -C {target} && rm -f {remote_tar}"
        )

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        sandbox = self._require_sandbox()
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        await sandbox.fs.download_file(source_path, str(target_path))

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        sandbox = self._require_sandbox()
        remote_tar = f"/tmp/harbor-download-{uuid.uuid4().hex}.tar.gz"
        await self._checked_exec(f"tar -czf {remote_tar} -C {shlex.quote(source_dir)} .")
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                local_tar = Path(tmp) / "download.tar.gz"
                await sandbox.fs.download_file(remote_tar, str(local_tar))
                with tarfile.open(local_tar, mode="r:gz") as tar:
                    tar.extractall(target, filter="data")
        finally:
            await self.exec(f"rm -f {remote_tar}", user="root")

    async def _checked_exec(self, command: str) -> None:
        result = await self.exec(command, user="root")
        if result.return_code != 0:
            raise RuntimeError(
                f"Daytona v2 command failed (exit {result.return_code}): {command}\n"
                f"{result.stderr or result.stdout}"
            )
