"""Unit tests for rho-agent's Daytona v2 (SDK 2.x) sandbox layer.

The SDK is mocked: these check how rho-agent maps its calls onto the v2 API.
The live tests in test_daytona_live.py check the service itself.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from rho_agent.tools.handlers.daytona.backend import DaytonaBackend, DockerfileImage
from rho_agent.tools.handlers.daytona.manager import SandboxManager
from rho_agent.tools.handlers.daytona.v2 import (
    TIMEOUT_EXIT_CODE,
    FileUpload,
    SandboxV2,
    combined_output,
    exit_code_of,
)


def _process_result(exit_code=0, reason="exited", stdout="", stderr="", output=""):
    return SimpleNamespace(
        exit_code=exit_code, reason=reason, stdout=stdout, stderr=stderr, output=output
    )


def _raw_sandbox():
    raw = MagicMock()
    raw.id = "sbx_test"
    raw.state = "started"
    raw.process.run = AsyncMock(return_value=_process_result(output="hi\n"))
    raw.fs.write_bytes = AsyncMock()
    raw.fs.upload_file = AsyncMock()
    raw.fs.write_many = AsyncMock()
    raw.fs.read_bytes = AsyncMock(return_value=b"data")
    raw.fs.download_file = AsyncMock()
    raw.delete = AsyncMock()
    return raw


# ---------------------------------------------------------------------------
# Result mapping
# ---------------------------------------------------------------------------


class TestResultMapping:
    def test_exit_code_reported(self):
        assert exit_code_of(_process_result(exit_code=3)) == 3

    def test_timed_out_is_124(self):
        assert (
            exit_code_of(_process_result(exit_code=None, reason="timed_out")) == TIMEOUT_EXIT_CODE
        )

    def test_unknown_end_is_minus_one(self):
        assert exit_code_of(_process_result(exit_code=None, reason="killed")) == -1

    def test_output_preferred_over_streams(self):
        assert combined_output(_process_result(output="both", stdout="o", stderr="e")) == "both"

    def test_streams_joined_without_output(self):
        assert combined_output(_process_result(output="", stdout="o", stderr="e")) == "oe"


# ---------------------------------------------------------------------------
# SandboxV2 adapter
# ---------------------------------------------------------------------------


class TestSandboxV2:
    async def test_exec_maps_to_run_with_deadline(self):
        raw = _raw_sandbox()
        sandbox = SandboxV2(raw)

        resp = await sandbox.process.exec("echo hi", cwd="/w", env={"A": "1"}, timeout=10)

        assert resp.result == "hi\n"
        assert resp.exit_code == 0
        raw.process.run.assert_awaited_once_with(
            "echo hi", cwd="/w", env={"A": "1"}, execution_timeout=10.0, timeout=40.0
        )

    async def test_exec_without_timeout_waits_unbounded(self):
        raw = _raw_sandbox()
        await SandboxV2(raw).process.exec("true")
        kwargs = raw.process.run.await_args.kwargs
        assert kwargs["execution_timeout"] is None and kwargs["timeout"] is None

    async def test_upload_bytes_writes_bytes(self):
        raw = _raw_sandbox()
        await SandboxV2(raw).fs.upload_file(b"abc", "/w/a.txt")
        raw.fs.write_bytes.assert_awaited_once_with("/w/a.txt", b"abc")
        raw.fs.upload_file.assert_not_awaited()

    async def test_upload_path_streams_file(self, tmp_path):
        raw = _raw_sandbox()
        local = tmp_path / "a.txt"
        local.write_text("x")
        await SandboxV2(raw).fs.upload_file(str(local), "/w/a.txt")
        raw.fs.upload_file.assert_awaited_once_with(str(local), "/w/a.txt")

    async def test_upload_files_batches(self, tmp_path):
        raw = _raw_sandbox()
        files = []
        for i in range(120):
            p = tmp_path / f"f{i}.txt"
            p.write_text(str(i))
            files.append(FileUpload(source=str(p), destination=f"/w/f{i}.txt"))
        files.append(FileUpload(source=b"raw", destination="/w/raw.bin"))

        await SandboxV2(raw).fs.upload_files(files)

        batches = [c.args[0] for c in raw.fs.write_many.await_args_list]
        assert [len(b) for b in batches] == [50, 50, 21]
        assert batches[0]["/w/f0.txt"] == b"0"
        assert batches[-1]["/w/raw.bin"] == b"raw"

    async def test_download_returns_bytes(self):
        raw = _raw_sandbox()
        assert await SandboxV2(raw).fs.download_file("/w/a.txt") == b"data"

    async def test_download_to_path(self, tmp_path):
        raw = _raw_sandbox()
        dest = tmp_path / "sub" / "a.txt"
        assert await SandboxV2(raw).fs.download_file("/w/a.txt", dest) is None
        raw.fs.download_file.assert_awaited_once_with("/w/a.txt", str(dest))
        assert dest.parent.is_dir()

    async def test_delete_and_id(self):
        raw = _raw_sandbox()
        sandbox = SandboxV2(raw)
        assert sandbox.id == "sbx_test"
        await sandbox.delete()
        raw.delete.assert_awaited_once()


# ---------------------------------------------------------------------------
# SandboxManager on v2
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_v2(monkeypatch):
    """Patch daytona.AsyncDaytona so the manager talks to a mock v2 client."""
    import daytona

    v2 = MagicMock()
    v2.images.pull = AsyncMock(return_value=SimpleNamespace(id="img_pulled", status="ready"))
    v2.images.build_from_context = AsyncMock(return_value=SimpleNamespace(id="img_built"))
    v2.images.get = AsyncMock(return_value=SimpleNamespace(id="img_built", status="ready"))
    raw = _raw_sandbox()
    # `which uv` succeeds, so no install runs.
    raw.process.run = AsyncMock(return_value=_process_result(output="/usr/bin/uv\n"))
    v2.create = AsyncMock(return_value=raw)
    client = MagicMock()
    client.v2 = v2
    client.close = AsyncMock()
    monkeypatch.setattr(daytona, "AsyncDaytona", MagicMock(return_value=client))
    return v2


class TestSandboxManagerV2:
    async def test_default_image_is_pulled(self, fake_v2):
        mgr = SandboxManager()
        sandbox = await mgr.get_sandbox()

        fake_v2.images.pull.assert_awaited_once()
        assert fake_v2.images.pull.await_args.args[0] == "daytonaio/sandbox:latest"
        args, kwargs = fake_v2.create.await_args
        assert args == ("img_pulled",)
        assert kwargs["env"]["PATH"].startswith("/home/daytona/.local/bin")
        assert kwargs["lifecycle_policy"]["on_timeout"] == "delete"
        assert "idle_expiry_seconds" not in kwargs["lifecycle_policy"]
        assert isinstance(sandbox, SandboxV2)

    async def test_snapshot_is_used_as_image_name(self, fake_v2):
        mgr = SandboxManager.from_backend(DaytonaBackend(snapshot="my-env"))
        await mgr.get_sandbox()
        fake_v2.images.pull.assert_not_awaited()
        assert fake_v2.create.await_args.args == ("my-env",)

    async def test_dockerfile_image_is_built(self, fake_v2, tmp_path):
        dockerfile = tmp_path / "Dockerfile.dev"
        dockerfile.write_text("FROM python:3.13\n")
        mgr = SandboxManager.from_backend(DaytonaBackend(image=DockerfileImage(str(dockerfile))))
        await mgr.get_sandbox()
        fake_v2.images.build_from_context.assert_awaited_once_with(
            str(tmp_path), dockerfile="Dockerfile.dev"
        )
        assert fake_v2.create.await_args.args == ("img_built",)

    async def test_resources_and_idle_timeout(self, fake_v2):
        resources = SimpleNamespace(cpu=8, memory=16, disk=20, gpu=None)
        backend = DaytonaBackend(resources=resources, auto_stop_interval=15)
        await SandboxManager.from_backend(backend).get_sandbox()
        kwargs = fake_v2.create.await_args.kwargs
        assert (kwargs["resources"].cpu, kwargs["resources"].memory, kwargs["resources"].disk) == (
            8,
            16,
            20,
        )
        assert kwargs["lifecycle_policy"]["idle_expiry_seconds"] == 900

    async def test_gpu_is_refused(self, fake_v2):
        backend = DaytonaBackend(resources=SimpleNamespace(cpu=4, memory=8, disk=10, gpu=1))
        with pytest.raises(ValueError, match="GPU"):
            await SandboxManager.from_backend(backend).get_sandbox()
        fake_v2.create.assert_not_awaited()

    async def test_sandbox_that_did_not_start_is_deleted(self, fake_v2):
        raw = fake_v2.create.return_value
        raw.state = "error"
        with pytest.raises(RuntimeError, match="did not start"):
            await SandboxManager().get_sandbox()
        raw.delete.assert_awaited_once()

    async def test_unready_image_raises(self, fake_v2):
        fake_v2.images.pull.return_value = SimpleNamespace(id="img_x", status="failed")
        with pytest.raises(RuntimeError, match="not ready"):
            await SandboxManager().get_sandbox()

    async def test_close_deletes_sandbox(self, fake_v2):
        mgr = SandboxManager()
        await mgr.get_sandbox()
        raw = fake_v2.create.return_value
        await mgr.close()
        raw.delete.assert_awaited_once()
        assert mgr._sandbox is None


# ---------------------------------------------------------------------------
# Daytona's injected git
# ---------------------------------------------------------------------------


class TestPreferImageGit:
    async def test_moved_when_image_has_git(self):
        from rho_agent.tools.handlers.daytona.v2 import prefer_image_git

        raw = _raw_sandbox()
        raw.process.run = AsyncMock(return_value=_process_result(stdout="moved\n"))
        assert await prefer_image_git(raw) is True
        args, kwargs = raw.process.run.await_args
        assert "/usr/local/bin/daytona-git" in args[0] and kwargs["user"] == "root"

    async def test_kept_when_image_has_no_git(self):
        from rho_agent.tools.handlers.daytona.v2 import prefer_image_git

        raw = _raw_sandbox()
        raw.process.run = AsyncMock(return_value=_process_result(stdout=""))
        assert await prefer_image_git(raw) is False

    async def test_errors_are_swallowed(self):
        from rho_agent.tools.handlers.daytona.v2 import prefer_image_git

        raw = _raw_sandbox()
        raw.process.run = AsyncMock(side_effect=RuntimeError("boom"))
        assert await prefer_image_git(raw) is False
