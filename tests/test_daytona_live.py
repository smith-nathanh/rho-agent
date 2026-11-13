"""Live smoke test against a real Daytona sandbox.

Creates (and deletes) one sandbox on your Daytona account, so it never runs by
accident. To run it:

    RHO_AGENT_LIVE_DAYTONA=1 DAYTONA_API_KEY=... uv run pytest tests/test_daytona_live.py -v

The other Daytona tests are mocked; this is the only one that can catch a
change in the SDK's or the service's real behaviour.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("RHO_AGENT_LIVE_DAYTONA") == "1" and os.environ.get("DAYTONA_API_KEY")),
    reason="set RHO_AGENT_LIVE_DAYTONA=1 and DAYTONA_API_KEY to run against Daytona",
)


async def test_every_remote_tool_round_trips() -> None:
    from rho_agent.tools.base import ToolInvocation
    from rho_agent.tools.handlers.daytona import register_daytona_tools
    from rho_agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    manager = register_daytona_tools(registry, working_dir="/home/daytona")

    async def call(tool: str, **arguments: Any):
        out = await registry.dispatch(
            ToolInvocation(call_id=tool, tool_name=tool, arguments=arguments)
        )
        assert out.success, f"{tool} failed: {out.content}"
        return out

    try:
        work = "/home/daytona/rho-smoke"
        out = await call("bash", command=f"mkdir -p {work} && echo ready")
        assert json.loads(out.content)["output"].strip() == "ready"

        await call("write", path=f"{work}/a.txt", content="alpha\nbeta\n")
        assert "beta" in (await call("read", path=f"{work}/a.txt")).content
        await call("edit", path=f"{work}/a.txt", old_string="beta", new_string="gamma")
        assert "gamma" in (await call("read", path=f"{work}/a.txt")).content
        assert "a.txt" in (await call("list", path=work)).content
        assert "a.txt" in (await call("glob", pattern="*.txt", path=work)).content
        assert "gamma" in (await call("grep", pattern="gamma", path=work)).content

        # Large output is kept in the sandbox and referenced.
        out = await call("bash", command="seq 1 20000")
        assert out.metadata.get("output_saved_in_sandbox") is True

        # Background job: returns at once, exit code lands in the log.
        out = await call("bash", command="sleep 1; echo bg-done", run_in_background=True)
        log = next(
            line.removeprefix("Log: ")
            for line in json.loads(out.content)["output"].splitlines()
            if line.startswith("Log: ")
        )
        out = await call("bash", command=f"sleep 3; cat {log}")
        assert "bg-done" in out.content and "[exit code: 0]" in out.content

        # A timeout kills the command and reports it.
        out = await registry.dispatch(
            ToolInvocation(
                call_id="t", tool_name="bash", arguments={"command": "sleep 60", "timeout": 2}
            )
        )
        assert out.success is False and out.metadata.get("timed_out") is True
    finally:
        await manager.close()


async def test_relative_paths_use_the_working_directory() -> None:
    """File tools and bash agree on where a relative path points."""
    from rho_agent.tools.base import ToolInvocation
    from rho_agent.tools.handlers.daytona import register_daytona_tools
    from rho_agent.tools.registry import ToolRegistry

    work = "/home/daytona/rho-smoke-rel/proj"
    registry = ToolRegistry()
    manager = register_daytona_tools(registry, working_dir=work)

    async def call(tool: str, **arguments: Any):
        out = await registry.dispatch(
            ToolInvocation(call_id=tool, tool_name=tool, arguments=arguments)
        )
        assert out.success, f"{tool} failed: {out.content}"
        return out

    try:
        await call("bash", command=f"mkdir -p {work}", working_dir="/home/daytona")

        await call("write", path="sub/a.txt", content="alpha\nbeta\n")
        out = await call("bash", command="pwd && cat sub/a.txt")
        assert work in out.content and "beta" in out.content

        assert "beta" in (await call("read", path="sub/a.txt")).content
        await call("edit", path="sub/a.txt", old_string="beta", new_string="gamma")
        out = await call("bash", command=f"cat {work}/sub/a.txt")
        assert "gamma" in out.content
        assert "sub" in (await call("list", path=".")).content
        assert "a.txt" in (await call("glob", pattern="*.txt", path=".")).content
        assert "gamma" in (await call("grep", pattern="gamma", path="sub")).content
    finally:
        await manager.close()
