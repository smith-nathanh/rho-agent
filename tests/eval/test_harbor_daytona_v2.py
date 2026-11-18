"""Tests for the Harbor environment on Daytona v2, with the SDK mocked."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("harbor", reason="harbor extra not installed")

from daytona.v2 import DISABLED
from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.paths import TrialPaths

from rho_agent.eval.harbor import daytona_v2
from rho_agent.eval.harbor.daytona_v2 import (
    DaytonaV2Environment,
    network_policy_input,
    sandbox_shape,
)


def _policy(mode: NetworkMode, hosts: list[str] | None = None) -> NetworkPolicy:
    return NetworkPolicy(network_mode=mode, allowed_hosts=hosts or [])


def _env(
    tmp_path: Path,
    *,
    docker_image: str | None = None,
    network: NetworkPolicy | None = None,
    **resources,
) -> DaytonaV2Environment:
    env_dir = tmp_path / "environment"
    env_dir.mkdir(exist_ok=True)
    if docker_image is None:
        (env_dir / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    config = EnvironmentConfig(docker_image=docker_image, **resources)
    return DaytonaV2Environment(
        environment_dir=env_dir,
        environment_name="task",
        session_id="task__abc__env",
        trial_paths=TrialPaths(trial_dir=tmp_path / "trial"),
        task_env_config=config,
        network_policy=network or _policy(NetworkMode.PUBLIC),
    )


def _process_result(exit_code=0, stdout="", stderr=""):
    return SimpleNamespace(
        exit_code=exit_code, reason="exited", stdout=stdout, stderr=stderr, output=""
    )


@pytest.fixture
def fake_v2(monkeypatch):
    import daytona

    v2 = MagicMock()
    v2.images.pull = AsyncMock(return_value=SimpleNamespace(id="img_pulled", status="ready"))
    v2.images.build_from_context = AsyncMock(return_value=SimpleNamespace(id="img_built"))
    v2.images.get = AsyncMock(return_value=SimpleNamespace(id="img_built", status="ready"))
    sandbox = MagicMock()
    sandbox.id = "sbx_1"
    sandbox.state = "started"
    sandbox.process.run = AsyncMock(return_value=_process_result())
    sandbox.wait = AsyncMock()
    sandbox.delete = AsyncMock()
    v2.create = AsyncMock(return_value=sandbox)
    client = MagicMock()
    client.v2 = v2
    client.close = AsyncMock()
    monkeypatch.setattr(daytona, "AsyncDaytona", MagicMock(return_value=client))
    return v2


# ---------------------------------------------------------------------------
# Network policy mapping
# ---------------------------------------------------------------------------


class TestNetworkPolicyInput:
    def test_public_has_no_policy(self):
        assert network_policy_input(_policy(NetworkMode.PUBLIC)) is None

    def test_no_network_blocks_all(self):
        assert network_policy_input(_policy(NetworkMode.NO_NETWORK)) == {"block_all": True}

    def test_allowlist_splits_hosts_and_ips(self):
        policy = _policy(NetworkMode.ALLOWLIST, ["api.openai.com", "10.0.0.1", "192.168.0.0/16"])
        assert network_policy_input(policy) == {
            "block_all": True,
            "allow_domains": ["api.openai.com"],
            "allow_cidrs": ["10.0.0.1/32", "192.168.0.0/16"],
        }

    def test_wildcard_is_refused(self):
        with pytest.raises(ValueError, match="cannot allow"):
            network_policy_input(_policy(NetworkMode.ALLOWLIST, ["*.example.com"]))


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


class TestDaytonaV2Environment:
    def test_type_and_capabilities(self, tmp_path):
        env = _env(tmp_path)
        assert env.type() == "daytona-v2"
        caps = env.capabilities
        assert caps.disable_internet and caps.network_allowlist and caps.dynamic_network_policy
        assert not caps.gpus

    def test_needs_dockerfile_or_image(self, tmp_path):
        env_dir = tmp_path / "environment"
        env_dir.mkdir()
        with pytest.raises(FileNotFoundError, match="Dockerfile"):
            DaytonaV2Environment(
                environment_dir=env_dir,
                environment_name="task",
                session_id="task__abc__env",
                trial_paths=TrialPaths(trial_dir=tmp_path / "trial"),
                task_env_config=EnvironmentConfig(),
            )

    async def test_start_builds_and_sizes_the_sandbox(self, tmp_path, fake_v2):
        env = _env(
            tmp_path,
            cpus=8,
            memory_mb=16384,
            storage_mb=20480,
            network=_policy(NetworkMode.NO_NETWORK),
        )
        await env.start(force_build=False)

        fake_v2.images.build_from_context.assert_awaited_once_with(str(tmp_path / "environment"))
        args, kwargs = fake_v2.create.await_args
        assert args == ("img_built",)
        res = kwargs["resources"]
        # 8 vCPUs is 4 Daytona cores; the max limits pin the sandbox to that shape.
        assert (res.cpu, res.cpu_max) == (4, 4)
        assert (res.memory, res.memory_max) == (16, 16)
        assert (res.disk, res.disk_max) == (20, 20)
        assert kwargs["network_policy"] == {"block_all": True}
        assert kwargs["wait_seconds"] == 0
        policy = kwargs["lifecycle_policy"]
        assert policy["timeout_seconds"] >= 12 * 3600
        assert policy["idle_expiry_seconds"] == DISABLED
        fake_v2.create.return_value.wait.assert_awaited_once()

    async def test_start_pulls_prebuilt_image(self, tmp_path, fake_v2):
        env = _env(tmp_path, docker_image="ghcr.io/example/task:1")
        await env.start(force_build=False)
        assert fake_v2.images.pull.await_args.args[0] == "ghcr.io/example/task:1"
        assert "network_policy" not in fake_v2.create.await_args.kwargs

    def test_shape_rounds_and_defaults(self):
        res = sandbox_shape(cpus=1, memory_mb=None, storage_mb=None)
        assert (res.cpu, res.memory, res.disk) == (0.5, 2, 10)
        assert sandbox_shape(cpus=3, memory_mb=4096, storage_mb=None).cpu == 1.5

    async def test_failed_start_deletes_sandbox(self, tmp_path, fake_v2):
        sandbox = fake_v2.create.return_value
        sandbox.wait.side_effect = RuntimeError("sandbox entered state error")
        env = _env(tmp_path)
        with pytest.raises(RuntimeError, match="state error"):
            await env.start(force_build=False)
        sandbox.delete.assert_awaited_once()
        assert env._sandbox is None

    async def test_exec_defaults_to_task_workdir(self, tmp_path, fake_v2):
        env = _env(tmp_path, workdir="/app")
        await env.start(force_build=False)
        sandbox = fake_v2.create.return_value
        await env.exec("ls")
        assert sandbox.process.run.await_args.kwargs["cwd"] == "/app"
        await env.exec("ls", cwd="/tmp")
        assert sandbox.process.run.await_args.kwargs["cwd"] == "/tmp"

    async def test_numeric_user_is_resolved_once(self, tmp_path, fake_v2):
        env = _env(tmp_path)
        await env.start(force_build=False)
        sandbox = fake_v2.create.return_value
        sandbox.process.run = AsyncMock(
            side_effect=[_process_result(stdout="agent\n"), _process_result(), _process_result()]
        )
        await env.exec("id", user=1000)
        await env.exec("id", user=1000)
        calls = sandbox.process.run.await_args_list
        assert calls[0].args[0].startswith("getent passwd 1000")
        assert calls[1].kwargs["user"] == calls[2].kwargs["user"] == "agent"
        assert len(calls) == 3

    async def test_unknown_numeric_user_raises(self, tmp_path, fake_v2):
        env = _env(tmp_path)
        await env.start(force_build=False)
        fake_v2.create.return_value.process.run = AsyncMock(return_value=_process_result())
        with pytest.raises(RuntimeError, match="UID 4242"):
            await env.exec("id", user=4242)

    async def test_exec_passes_user_env_and_deadline(self, tmp_path, fake_v2):
        env = _env(tmp_path)
        await env.start(force_build=False)
        sandbox = fake_v2.create.return_value
        sandbox.process.run = AsyncMock(
            return_value=_process_result(exit_code=2, stdout="o", stderr="e")
        )

        result = await env.exec("ls", cwd="/app", env={"A": "1"}, timeout_sec=30, user="agent")

        assert (result.return_code, result.stdout, result.stderr) == (2, "o", "e")
        kwargs = sandbox.process.run.await_args.kwargs
        assert kwargs["user"] == "agent" and kwargs["cwd"] == "/app"
        assert kwargs["env"]["A"] == "1"
        assert kwargs["execution_timeout"] == 30.0

    async def test_switch_policy_waits_until_applied(self, tmp_path, fake_v2, monkeypatch):
        monkeypatch.setattr(daytona_v2.asyncio, "sleep", AsyncMock())
        env = _env(tmp_path)
        await env.start(force_build=False)
        sandbox = fake_v2.create.return_value
        pending = SimpleNamespace(
            etag='"e2"', desired_revision=2, applied_revision=1, error=None, status="pending"
        )
        applied = SimpleNamespace(
            etag='"e2"', desired_revision=2, applied_revision=2, error=None, status="applied"
        )
        sandbox.get_network_policy = AsyncMock(side_effect=[SimpleNamespace(etag='"e1"'), applied])
        sandbox.update_network_policy = AsyncMock(return_value=pending)

        await env.set_network_policy(_policy(NetworkMode.ALLOWLIST, ["api.openai.com"]))

        sandbox.update_network_policy.assert_awaited_once_with(
            network_policy={
                "block_all": True,
                "allow_domains": ["api.openai.com"],
                "allow_cidrs": [],
            },
            if_match='"e1"',
        )
        assert env.network_policy.network_mode == NetworkMode.ALLOWLIST

    async def test_switch_to_public_resets_policy(self, tmp_path, fake_v2):
        env = _env(tmp_path, network=_policy(NetworkMode.NO_NETWORK))
        await env.start(force_build=False)
        sandbox = fake_v2.create.return_value
        sandbox.get_network_policy = AsyncMock(return_value=SimpleNamespace(etag='"e1"'))
        sandbox.reset_network_policy = AsyncMock(
            return_value=SimpleNamespace(
                desired_revision=2, applied_revision=2, error=None, status="applied"
            )
        )
        await env.set_network_policy(_policy(NetworkMode.PUBLIC))
        sandbox.reset_network_policy.assert_awaited_once_with(if_match='"e1"')

    async def test_policy_error_raises(self, tmp_path, fake_v2):
        env = _env(tmp_path)
        await env.start(force_build=False)
        sandbox = fake_v2.create.return_value
        sandbox.get_network_policy = AsyncMock(return_value=SimpleNamespace(etag='"e1"'))
        sandbox.update_network_policy = AsyncMock(
            return_value=SimpleNamespace(
                desired_revision=2, applied_revision=None, error="bad", status="failed"
            )
        )
        with pytest.raises(RuntimeError, match="bad"):
            await env.set_network_policy(_policy(NetworkMode.NO_NETWORK))

    async def test_error_of_a_later_revision_is_not_ours(self, tmp_path, fake_v2, monkeypatch):
        monkeypatch.setattr(daytona_v2.asyncio, "sleep", AsyncMock())
        env = _env(tmp_path)
        await env.start(force_build=False)
        sandbox = fake_v2.create.return_value
        # Revision 2 is ours; a later revision 3 from elsewhere fails, then applies.
        pending = SimpleNamespace(
            etag='"e2"', desired_revision=2, applied_revision=1, error=None, status="pending"
        )
        theirs = SimpleNamespace(desired_revision=3, applied_revision=1, error="x", status="x")
        applied = SimpleNamespace(desired_revision=3, applied_revision=3, error=None)
        sandbox.get_network_policy = AsyncMock(
            side_effect=[SimpleNamespace(etag='"e1"'), theirs, applied]
        )
        sandbox.update_network_policy = AsyncMock(return_value=pending)
        await env._apply_network_policy(_policy(NetworkMode.NO_NETWORK))
        assert sandbox.get_network_policy.await_count == 3

    async def test_policy_not_applied_times_out(self, tmp_path, fake_v2, monkeypatch):
        monkeypatch.setattr(daytona_v2, "_POLICY_APPLY_SECONDS", -1.0)
        env = _env(tmp_path)
        await env.start(force_build=False)
        sandbox = fake_v2.create.return_value
        sandbox.get_network_policy = AsyncMock(return_value=SimpleNamespace(etag='"e1"'))
        sandbox.update_network_policy = AsyncMock(
            return_value=SimpleNamespace(
                desired_revision=2, applied_revision=1, error=None, status="pending"
            )
        )
        with pytest.raises(TimeoutError, match="revision 2"):
            await env.set_network_policy(_policy(NetworkMode.NO_NETWORK))

    async def test_upload_dir_extracts_without_owner(self, tmp_path, fake_v2):
        env = _env(tmp_path)
        await env.start(force_build=False)
        sandbox = fake_v2.create.return_value
        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        (src / "sub" / "a.txt").write_text("hello")
        uploaded = {}

        async def upload_file(local, remote):
            with tarfile.open(local) as tar:
                uploaded[remote] = sorted(tar.getnames())

        sandbox.fs.upload_file = AsyncMock(side_effect=upload_file)
        sandbox.process.run = AsyncMock(return_value=_process_result())

        await env.upload_dir(src, "/app/data dir")

        ((remote_tar, names),) = uploaded.items()
        assert "./sub/a.txt" in names
        commands = [c.args[0] for c in sandbox.process.run.await_args_list]
        assert any(
            f"tar --no-same-owner -xzf {remote_tar} -C '/app/data dir'" in c for c in commands
        )
        assert commands[-1] == f"rm -f {remote_tar}"

    async def test_download_dir_skips_refused_members(self, tmp_path, fake_v2):
        env = _env(tmp_path)
        await env.start(force_build=False)
        sandbox = fake_v2.create.return_value
        # What the sandbox would pack: a file, and a symlink out of the directory.
        packed = tmp_path / "packed.tar.gz"
        with tarfile.open(packed, "w:gz") as tar:
            data = b"result"
            info = tarfile.TarInfo("out/result.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            link = tarfile.TarInfo("out/passwd")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)

        async def download_file(remote, local):
            Path(local).write_bytes(packed.read_bytes())

        sandbox.fs.download_file = AsyncMock(side_effect=download_file)
        sandbox.process.run = AsyncMock(return_value=_process_result())

        target = tmp_path / "downloaded"
        await env.download_dir("/logs", target)

        assert (target / "out" / "result.txt").read_text() == "result"
        assert not (target / "out" / "passwd").exists()
        assert sandbox.process.run.await_args.args[0].startswith("rm -f /tmp/harbor-download-")

    async def test_failed_pack_still_cleans_up(self, tmp_path, fake_v2):
        env = _env(tmp_path)
        await env.start(force_build=False)
        sandbox = fake_v2.create.return_value
        sandbox.process.run = AsyncMock(
            side_effect=[_process_result(exit_code=2, stderr="no such dir"), _process_result()]
        )
        with pytest.raises(RuntimeError):
            await env.download_dir("/missing", tmp_path / "d")
        assert sandbox.process.run.await_args.args[0].startswith("rm -f ")

    async def test_stop_deletes(self, tmp_path, fake_v2):
        env = _env(tmp_path)
        await env.start(force_build=False)
        await env.stop(delete=True)
        fake_v2.create.return_value.delete.assert_awaited_once()
