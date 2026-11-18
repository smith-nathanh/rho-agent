"""Unit tests for rho-agent's Daytona v2 (SDK 2.x) helpers and sandbox manager.

The SDK is mocked: these check how rho-agent maps its calls onto the v2 API.
The live tests in test_daytona_live.py check the service itself.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from daytona.v2 import DISABLED

from rho_agent.tools.handlers.daytona.backend import (
    DaytonaBackend,
    DockerfileImage,
    RegisteredImage,
)
from rho_agent.tools.handlers.daytona.manager import SandboxManager
from rho_agent.tools.handlers.daytona.ops import (
    TIMEOUT_EXIT_CODE,
    combined_output,
    exit_code_of,
    prefer_image_git,
    run,
    upload_paths,
)


def _process_result(exit_code=0, reason="exited", stdout="", stderr="", output=""):
    return SimpleNamespace(
        exit_code=exit_code, reason=reason, stdout=stdout, stderr=stderr, output=output
    )


def _sandbox():
    sandbox = MagicMock()
    sandbox.id = "sbx_test"
    sandbox.state = "started"
    sandbox.process.run = AsyncMock(return_value=_process_result(output="hi\n"))
    sandbox.fs.write_many = AsyncMock()
    sandbox.wait = AsyncMock()
    sandbox.delete = AsyncMock()
    return sandbox


# ---------------------------------------------------------------------------
# Result mapping and run()
# ---------------------------------------------------------------------------


class TestRun:
    def test_exit_code_reported(self):
        assert exit_code_of(_process_result(exit_code=3)) == 3

    def test_timed_out_is_124(self):
        result = _process_result(exit_code=None, reason="timed_out")
        assert exit_code_of(result) == TIMEOUT_EXIT_CODE

    def test_unknown_end_is_minus_one(self):
        assert exit_code_of(_process_result(exit_code=None, reason="killed")) == -1

    @pytest.mark.parametrize("name", ["SIGKILL", "KILL", "kill"])
    def test_killed_by_signal_is_128_plus_n(self, name):
        result = _process_result(exit_code=None, reason="signaled")
        result.signal = name
        assert exit_code_of(result) == 137

    def test_unknown_signal_is_minus_one(self):
        result = _process_result(exit_code=None, reason="signaled")
        result.signal = "SIGNOPE"
        assert exit_code_of(result) == -1

    def test_output_preferred_over_streams(self):
        assert combined_output(_process_result(output="both", stdout="o", stderr="e")) == "both"

    def test_streams_joined_without_output(self):
        assert combined_output(_process_result(output="", stdout="o", stderr="e")) == "oe"

    async def test_run_sets_server_deadline(self):
        sandbox = _sandbox()
        result = await run(sandbox, "echo hi", cwd="/w", env={"A": "1"}, timeout=10)
        assert (result.output, result.exit_code) == ("hi\n", 0)
        sandbox.process.run.assert_awaited_once_with(
            "echo hi", cwd="/w", env={"A": "1"}, execution_timeout=10.0, timeout=40.0
        )

    async def test_run_without_kill_only_bounds_the_wait(self):
        sandbox = _sandbox()
        await run(sandbox, "sleep 600 &", timeout=10, kill_on_timeout=False)
        kwargs = sandbox.process.run.await_args.kwargs
        assert kwargs["execution_timeout"] is None and kwargs["timeout"] == 40.0

    async def test_run_without_timeout_waits_unbounded(self):
        sandbox = _sandbox()
        await run(sandbox, "true")
        kwargs = sandbox.process.run.await_args.kwargs
        assert kwargs["execution_timeout"] is None and kwargs["timeout"] is None

    async def test_upload_paths_batches(self, tmp_path):
        sandbox = _sandbox()
        files = []
        for i in range(120):
            p = tmp_path / f"f{i}.txt"
            p.write_text(str(i))
            files.append((p, f"/w/f{i}.txt"))

        await upload_paths(sandbox, files)

        batches = [c.args[0] for c in sandbox.fs.write_many.await_args_list]
        assert [len(b) for b in batches] == [50, 50, 20]
        assert batches[0]["/w/f0.txt"] == b"0"


# ---------------------------------------------------------------------------
# Daytona's injected git
# ---------------------------------------------------------------------------


class TestPreferImageGit:
    async def test_moved_when_image_has_git(self):
        sandbox = _sandbox()
        sandbox.process.run = AsyncMock(return_value=_process_result(stdout="moved\n"))
        assert await prefer_image_git(sandbox) is True
        args, kwargs = sandbox.process.run.await_args
        assert "/usr/local/bin/daytona-git" in args[0] and kwargs["user"] == "root"

    async def test_kept_when_image_has_no_git(self):
        sandbox = _sandbox()
        sandbox.process.run = AsyncMock(return_value=_process_result(stdout=""))
        assert await prefer_image_git(sandbox) is False

    async def test_errors_are_swallowed(self):
        sandbox = _sandbox()
        sandbox.process.run = AsyncMock(side_effect=RuntimeError("boom"))
        assert await prefer_image_git(sandbox) is False


# ---------------------------------------------------------------------------
# SandboxManager
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_v2(monkeypatch):
    """Patch daytona.AsyncDaytona so the manager talks to a mock v2 client."""
    import daytona

    v2 = MagicMock()
    v2.images.pull = AsyncMock(return_value=SimpleNamespace(id="img_pulled", status="ready"))
    v2.images.build_from_context = AsyncMock(return_value=SimpleNamespace(id="img_built"))
    v2.images.get = AsyncMock(return_value=SimpleNamespace(id="img_built", status="ready"))
    sandbox = _sandbox()
    # `which uv` succeeds, so no install runs; the git check reports nothing moved.
    sandbox.process.run = AsyncMock(return_value=_process_result(output="/usr/bin/uv\n"))
    v2.create = AsyncMock(return_value=sandbox)
    client = MagicMock()
    client.v2 = v2
    client.close = AsyncMock()
    monkeypatch.setattr(daytona, "AsyncDaytona", MagicMock(return_value=client))
    return v2


class TestSandboxManager:
    async def test_default_image_is_pulled(self, fake_v2):
        mgr = SandboxManager()
        sandbox = await mgr.get_sandbox()

        assert fake_v2.images.pull.await_args.args[0] == "daytonaio/sandbox:latest"
        args, kwargs = fake_v2.create.await_args
        assert args == ("img_pulled",)
        assert kwargs["env"]["PATH"].startswith("/home/daytona/.local/bin")
        assert kwargs["lifecycle_policy"]["on_timeout"] == "delete"
        # Off explicitly, so a project default idle timeout does not apply.
        assert kwargs["lifecycle_policy"]["idle_expiry_seconds"] == DISABLED
        assert kwargs["wait_seconds"] == 0
        sandbox.wait.assert_awaited_once()
        assert sandbox is fake_v2.create.return_value

    async def test_registered_image_boots_without_pull(self, fake_v2):
        backend = DaytonaBackend(image=RegisteredImage("my-env"))
        await SandboxManager.from_backend(backend).get_sandbox()
        fake_v2.images.pull.assert_not_awaited()
        assert fake_v2.create.await_args.args == ("my-env",)

    async def test_dockerfile_image_is_built(self, fake_v2, tmp_path):
        dockerfile = tmp_path / "Dockerfile.dev"
        dockerfile.write_text("FROM python:3.13\n")
        backend = DaytonaBackend(image=DockerfileImage(str(dockerfile)))
        await SandboxManager.from_backend(backend).get_sandbox()
        fake_v2.images.build_from_context.assert_awaited_once_with(
            str(tmp_path), dockerfile="Dockerfile.dev"
        )
        assert fake_v2.create.await_args.args == ("img_built",)

    async def test_resources_and_idle_timeout(self, fake_v2):
        from daytona.v2 import Resources

        resources = Resources(cpu=8, memory=16, disk=20)
        backend = DaytonaBackend(resources=resources, idle_timeout_minutes=15)
        await SandboxManager.from_backend(backend).get_sandbox()
        kwargs = fake_v2.create.await_args.kwargs
        assert kwargs["resources"] is resources
        assert kwargs["lifecycle_policy"]["idle_expiry_seconds"] == 900
        assert kwargs["lifecycle_policy"]["on_idle_timeout"] == "delete"

    async def test_unknown_image_type_is_refused(self, fake_v2):
        with pytest.raises(TypeError, match="RegisteredImage"):
            await SandboxManager(image=object()).get_sandbox()
        fake_v2.create.assert_not_awaited()

    async def test_sandbox_that_did_not_start_is_deleted(self, fake_v2):
        sandbox = fake_v2.create.return_value
        sandbox.wait.side_effect = RuntimeError("sandbox entered state error")
        mgr = SandboxManager()
        with pytest.raises(RuntimeError, match="state error"):
            await mgr.get_sandbox()
        sandbox.delete.assert_awaited_once()
        assert mgr._sandbox is None

    async def test_sandbox_is_recorded_while_it_boots(self, fake_v2):
        # Cancelled mid-boot: close() must still find the sandbox to delete it.
        sandbox = fake_v2.create.return_value
        sandbox.wait.side_effect = asyncio.CancelledError
        mgr = SandboxManager()
        with pytest.raises(asyncio.CancelledError):
            await mgr.get_sandbox()
        assert mgr._sandbox is None  # never handed out before it is ready
        await mgr.close()
        sandbox.delete.assert_awaited_once()

    async def test_unready_image_raises(self, fake_v2):
        fake_v2.images.pull.return_value = SimpleNamespace(id="img_x", status="failed")
        with pytest.raises(RuntimeError, match="not ready"):
            await SandboxManager().get_sandbox()

    async def test_uv_installed_when_missing(self, fake_v2):
        sandbox = fake_v2.create.return_value
        sandbox.process.run = AsyncMock(
            side_effect=[
                _process_result(stdout=""),  # git check: nothing moved
                _process_result(exit_code=1),  # which uv
                _process_result(output="installed"),  # install
            ]
        )
        await SandboxManager(uv_version="0.6.6").get_sandbox()
        install = sandbox.process.run.await_args_list[-1].args[0]
        assert "astral.sh/uv/0.6.6/install.sh" in install

    async def test_absolute_path_expands_home(self, fake_v2):
        mgr = SandboxManager(working_dir="/w")
        sandbox = await mgr.get_sandbox()
        sandbox.process.run = AsyncMock(return_value=_process_result(output="/home/daytona"))
        assert await mgr.absolute_path("~/a/../b.txt") == "/home/daytona/b.txt"
        assert await mgr.absolute_path("~") == "/home/daytona"
        assert await mgr.absolute_path("rel") == "/w/rel"
        assert await mgr.absolute_path("~user/x") == "~user/x"  # left to the shell
        sandbox.process.run.assert_awaited_once()  # $HOME looked up once

    async def test_close_deletes_sandbox(self, fake_v2):
        mgr = SandboxManager()
        await mgr.get_sandbox()
        await mgr.close()
        fake_v2.create.return_value.delete.assert_awaited_once()
        assert mgr._sandbox is None
