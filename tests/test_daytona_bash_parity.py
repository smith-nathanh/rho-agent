"""Daytona bash wrapper scripts, executed with the local shell.

The handler sends a wrapper script to the sandbox. Running that script through
local bash checks the logic (saved output, background jobs, timeouts) without a
Daytona account.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rho_agent.tools.base import ToolInvocation
from rho_agent.tools.handlers.daytona import bash as daytona_bash
from rho_agent.tools.handlers.daytona.bash import DaytonaBashHandler
from rho_agent.tools.handlers.daytona.manager import SandboxManager


class _LocalProcess:
    async def exec(
        self, command: str, cwd: str | None = None, env: Any = None, timeout: int | None = None
    ) -> SimpleNamespace:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return SimpleNamespace(result=out.decode(), exit_code=proc.returncode)


@pytest.fixture
def handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DaytonaBashHandler:
    monkeypatch.setattr(daytona_bash, "OUTPUT_DIR", str(tmp_path / "out"))
    manager = SandboxManager(working_dir=str(tmp_path))
    manager._sandbox = SimpleNamespace(process=_LocalProcess())
    return DaytonaBashHandler(manager)


async def _run(handler: DaytonaBashHandler, **arguments: Any):
    out = await handler.handle(ToolInvocation(call_id="c", tool_name="bash", arguments=arguments))
    return out, json.loads(out.content)


async def test_small_output_and_exit_code_pass_through(handler, tmp_path: Path) -> None:
    out, body = await _run(handler, command="echo one; echo two >&2; exit 3")
    assert body["output"] == "one\ntwo\n"
    assert body["metadata"]["exit_code"] == 3
    assert out.success is False
    assert list((tmp_path / "out").iterdir()) == []


async def test_quotes_and_newlines_in_the_command_survive(handler) -> None:
    _, body = await _run(handler, command='x=\'a "b" c\'\necho "$x" | tr a-z A-Z')
    assert body["output"] == 'A "B" C\n'


async def test_large_output_is_saved_in_the_sandbox(handler, tmp_path: Path) -> None:
    out, body = await _run(handler, command="seq 1 20000")
    (saved,) = list((tmp_path / "out").iterdir())
    assert saved.read_text().splitlines()[-1] == "20000"
    assert str(saved) in body["output"]
    assert body["output"].startswith("1\n2\n")
    assert "...output truncated..." in body["output"]
    assert out.metadata["output_saved_in_sandbox"] is True
    assert out.success is True
    assert len(out.content.encode()) < 5000 * 4  # fits the session limit intact


async def test_background_job_returns_at_once_and_logs_exit_code(handler, tmp_path: Path) -> None:
    out, body = await _run(
        handler, command="sleep 0.5; echo finished; exit 4", run_in_background=True
    )
    assert out.success is True and out.metadata["background"] is True
    assert out.metadata["duration_seconds"] < 0.4
    log = next(
        Path(line.removeprefix("Log: "))
        for line in body["output"].splitlines()
        if line.startswith("Log: ")
    )
    for _ in range(40):
        if log.exists() and "[exit code:" in log.read_text():
            break
        await asyncio.sleep(0.1)
    assert log.read_text() == "finished\n[exit code: 4]\n"


@pytest.mark.skipif(shutil.which("timeout") is None, reason="needs coreutils timeout")
async def test_timeout_kills_child_processes(handler, tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    out, body = await _run(handler, command=f"sleep 60 & echo $! > {pidfile}; wait", timeout=1)
    assert out.metadata["timed_out"] is True
    assert "run_in_background" in body["output"]
    child = int(pidfile.read_text())
    for _ in range(20):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("child process survived the timeout")
