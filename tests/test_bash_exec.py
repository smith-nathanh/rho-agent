"""Tests for bash execution: process groups, background jobs, saved output."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from rho_agent.tools.base import ToolInvocation
from rho_agent.tools.handlers.bash import BashHandler


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _run(handler: BashHandler, **arguments):
    out = await handler.handle(ToolInvocation(call_id="c", tool_name="bash", arguments=arguments))
    return out, json.loads(out.content) if out.content.startswith("{") else None


async def test_stdout_and_stderr_are_captured_in_order(tmp_path: Path) -> None:
    handler = BashHandler(working_dir=str(tmp_path))
    out, body = await _run(handler, command="echo one; echo two >&2; echo three; exit 3")
    assert body["output"] == "one\ntwo\nthree\n"
    assert body["metadata"]["exit_code"] == 3
    assert out.success is False


async def test_model_api_key_is_hidden_from_commands(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RHO_AGENT_API_KEY", "sk-test-hidden")
    monkeypatch.setenv("RHO_TEST_VISIBLE", "still-here")
    handler = BashHandler(working_dir=str(tmp_path))

    for arguments in (
        {"command": "env"},
        {"command": "printenv RHO_AGENT_API_KEY; echo rc=$?"},
    ):
        out, body = await _run(handler, **arguments)
        assert "sk-test-hidden" not in out.content
    assert "rc=1" in body["output"]

    # Other variables, and the agent's own process, are unchanged.
    out, body = await _run(handler, command="printenv RHO_TEST_VISIBLE")
    assert body["output"] == "still-here\n"
    assert os.environ["RHO_AGENT_API_KEY"] == "sk-test-hidden"


async def test_mcp_credentials_are_hidden_from_commands(tmp_path: Path, monkeypatch) -> None:
    # Harbor passes MCP server headers in MCP_AUTH_* variables.
    monkeypatch.setenv("MCP_AUTH_ABC123_0_0", "Bearer mcp-secret")
    handler = BashHandler(working_dir=str(tmp_path))
    out, _ = await _run(handler, command="env")
    assert "mcp-secret" not in out.content and "PATH=" in out.content
    assert os.environ["MCP_AUTH_ABC123_0_0"] == "Bearer mcp-secret"


async def test_model_api_key_is_hidden_from_background_jobs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RHO_AGENT_API_KEY", "sk-test-hidden")
    handler = BashHandler(working_dir=str(tmp_path))
    out_file = tmp_path / "env.txt"

    await _run(handler, command=f"env > {out_file}", run_in_background=True)
    for _ in range(50):
        if out_file.exists() and out_file.read_text():
            break
        await asyncio.sleep(0.1)

    assert "PATH=" in out_file.read_text()
    assert "sk-test-hidden" not in out_file.read_text()


async def test_timeout_kills_child_processes(tmp_path: Path) -> None:
    handler = BashHandler(working_dir=str(tmp_path))
    pidfile = tmp_path / "child.pid"
    out, body = await _run(handler, command=f"sleep 60 & echo $! > {pidfile}; wait", timeout=1)
    assert out.metadata["timed_out"] is True
    assert "run_in_background" in body["output"]
    child = int(pidfile.read_text())
    for _ in range(20):
        if not _alive(child):
            break
        await asyncio.sleep(0.1)
    assert not _alive(child)


async def test_command_returns_when_shell_exits_despite_running_child(tmp_path: Path) -> None:
    handler = BashHandler(working_dir=str(tmp_path))
    start = time.perf_counter()
    out, body = await _run(handler, command="sleep 20 & echo started", timeout=15)
    assert time.perf_counter() - start < 5
    assert out.success is True
    assert "started" in body["output"]


async def test_large_output_is_saved_and_referenced(tmp_path: Path) -> None:
    handler = BashHandler(working_dir=str(tmp_path))
    out, body = await _run(handler, command="seq 1 20000")
    saved = Path(out.metadata["full_output_path"])
    assert saved.exists()
    assert saved.read_text().splitlines()[-1] == "20000"
    assert str(saved) in body["output"]
    assert "tokens truncated" in body["output"]
    assert body["output"].startswith("1\n2\n")
    # Small enough that the session-level truncation leaves the JSON intact.
    assert len(out.content.encode()) < 5000 * 4


async def test_small_output_leaves_no_file(tmp_path: Path) -> None:
    handler = BashHandler(working_dir=str(tmp_path))
    out, _ = await _run(handler, command="echo hi")
    assert "full_output_path" not in out.metadata
    assert list(handler._output_dir.iterdir()) == []


async def test_background_job_returns_at_once_and_logs_exit_code(tmp_path: Path) -> None:
    handler = BashHandler(working_dir=str(tmp_path))
    start = time.perf_counter()
    out, body = await _run(
        handler, command="sleep 0.5; echo finished; exit 4", run_in_background=True
    )
    assert time.perf_counter() - start < 0.4
    assert out.success is True and out.metadata["background"] is True
    log = Path(out.metadata["log_path"])
    assert str(log) in body["output"] and str(out.metadata["pid"]) in body["output"]
    for _ in range(40):
        if log.exists() and "[exit code:" in log.read_text():
            break
        await asyncio.sleep(0.1)
    assert log.read_text() == "finished\n[exit code: 4]\n"


async def test_cancel_kills_the_process_group(tmp_path: Path) -> None:
    handler = BashHandler(working_dir=str(tmp_path))
    pidfile = tmp_path / "child.pid"
    task = asyncio.ensure_future(
        _run(handler, command=f"sleep 60 & echo $! > {pidfile}; wait", timeout=30)
    )
    for _ in range(40):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    child = int(pidfile.read_text())
    for _ in range(20):
        if not _alive(child):
            break
        await asyncio.sleep(0.1)
    assert not _alive(child)
