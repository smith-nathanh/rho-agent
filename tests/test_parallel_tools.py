"""Tests for running a model turn's tool calls concurrently."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

from rho_agent.client.model import StreamEvent, ToolCall
from rho_agent.core.agent import Agent
from rho_agent.core.config import AgentConfig
from rho_agent.core.session import Session
from rho_agent.permissions import PermissionProfile
from rho_agent.permissions.factory import ToolFactory
from rho_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput
from rho_agent.tools.registry import ToolRegistry


class _Sleepy(ToolHandler):
    """Sleeps for the requested time, recording when it started and finished."""

    def __init__(self, name: str = "sleepy", safe: bool = True, approval: bool = False) -> None:
        self._name = name
        self._safe = safe
        self._approval = approval
        self.log: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "sleep"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"s": {"type": "number"}, "tag": {"type": "string"}},
        }

    @property
    def parallel_safe(self) -> bool:
        return self._safe

    @property
    def requires_approval(self) -> bool:
        return self._approval

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        tag = invocation.arguments["tag"]
        self.log.append(("start", tag))
        await asyncio.sleep(invocation.arguments.get("s", 0))
        self.log.append(("end", tag))
        return ToolOutput(content=f"result-{tag}")


class _Client:
    def __init__(self, *calls: list[StreamEvent]) -> None:
        self._calls = list(calls)

    async def stream(self, prompt: Any):
        events = (
            self._calls.pop(0)
            if self._calls
            else [
                StreamEvent(type="text", content="done"),
                StreamEvent(type="done", usage={"input_tokens": 1, "output_tokens": 1}),
            ]
        )
        for e in events:
            yield e


def _turn(*calls: tuple[str, str, dict[str, Any]]) -> list[StreamEvent]:
    events = [
        StreamEvent(type="tool_call", tool_call=ToolCall(id=i, name=n, arguments=a))
        for i, n, a in calls
    ]
    return [*events, StreamEvent(type="done", usage={"input_tokens": 1, "output_tokens": 1})]


def _session(client: _Client, *handlers: ToolHandler) -> Session:
    registry = ToolRegistry()
    for h in handlers:
        registry.register(h)
    agent = MagicMock(spec=Agent)
    agent.config = AgentConfig(system_prompt="sys", model="test-model")
    agent.system_prompt = "sys"
    agent.registry = registry
    agent.sandbox_manager = None
    return Session(agent, client=client)


def _tool_results(session: Session) -> list[tuple[str, str]]:
    return [
        (m["tool_call_id"], m["content"]) for m in session.state.messages if m["role"] == "tool"
    ]


async def test_safe_calls_run_concurrently_and_results_keep_call_order() -> None:
    tool = _Sleepy()
    client = _Client(
        _turn(
            ("a", "sleepy", {"s": 0.3, "tag": "a"}),
            ("b", "sleepy", {"s": 0.1, "tag": "b"}),
            ("c", "sleepy", {"s": 0.2, "tag": "c"}),
        )
    )
    session = _session(client, tool)

    start = time.perf_counter()
    result = await session.run("go")
    elapsed = time.perf_counter() - start

    assert result.status == "completed"
    assert elapsed < 0.55  # serial would be 0.6s
    assert [e for e, _ in tool.log[:3]] == ["start", "start", "start"]
    assert _tool_results(session) == [("a", "result-a"), ("b", "result-b"), ("c", "result-c")]


async def test_a_single_unsafe_call_makes_the_whole_turn_serial() -> None:
    safe, unsafe = _Sleepy("safe"), _Sleepy("unsafe", safe=False)
    shared: list[tuple[str, str]] = []
    safe.log = unsafe.log = shared
    client = _Client(
        _turn(("a", "safe", {"s": 0.05, "tag": "a"}), ("b", "unsafe", {"s": 0.05, "tag": "b"}))
    )
    session = _session(client, safe, unsafe)

    await session.run("go")

    assert shared == [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")]


async def test_calls_needing_approval_are_not_run_in_parallel() -> None:
    tool = _Sleepy(approval=True)
    client = _Client(
        _turn(("a", "sleepy", {"s": 0.05, "tag": "a"}), ("b", "sleepy", {"s": 0.05, "tag": "b"}))
    )
    session = _session(client, tool)
    asked: list[str] = []

    async def approve(name: str, args: dict[str, Any]) -> bool:
        asked.append(args["tag"])
        return True

    session.approval_callback = approve
    await session.run("go")

    assert asked == ["a", "b"]
    assert tool.log == [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")]


async def test_parallel_can_be_switched_off() -> None:
    tool = _Sleepy()
    client = _Client(
        _turn(("a", "sleepy", {"s": 0.05, "tag": "a"}), ("b", "sleepy", {"s": 0.05, "tag": "b"}))
    )
    session = _session(client, tool)
    session.parallel_tool_calls = False
    await session.run("go")
    assert tool.log == [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")]


async def test_time_cap_stops_a_parallel_batch_and_pairs_every_call() -> None:
    tool = _Sleepy()
    client = _Client(
        _turn(("a", "sleepy", {"s": 0.05, "tag": "a"}), ("b", "sleepy", {"s": 30, "tag": "b"}))
    )
    session = _session(client, tool)
    session.max_wall_seconds = 0.4

    result = await asyncio.wait_for(session.run("go"), timeout=5)

    assert result.status == "budget_exceeded"
    results = dict(_tool_results(session))
    assert results["a"] == "result-a"
    assert "time cap" in results["b"]
    assert session.state.pending_tool_call_ids() == []


async def test_cancel_stops_a_parallel_batch() -> None:
    tool = _Sleepy()
    client = _Client(
        _turn(("a", "sleepy", {"s": 30, "tag": "a"}), ("b", "sleepy", {"s": 30, "tag": "b"}))
    )
    session = _session(client, tool)

    async def cancel_soon() -> None:
        await asyncio.sleep(0.2)
        session._cancelled = True

    canceller = asyncio.ensure_future(cancel_soon())
    result = await asyncio.wait_for(session.run("go"), timeout=5)
    await canceller

    assert result.status == "cancelled"
    assert session.state.pending_tool_call_ids() == []


def test_builtin_tools_declare_parallel_safety() -> None:
    registry = ToolFactory(PermissionProfile.developer()).create_registry(working_dir="/tmp")
    assert registry.is_parallel_safe("read")
    assert registry.is_parallel_safe("grep")
    assert registry.is_parallel_safe("glob")
    assert registry.is_parallel_safe("list")
    assert not registry.is_parallel_safe("bash")
    assert not registry.is_parallel_safe("write")
    assert not registry.is_parallel_safe("edit")
    assert not registry.is_parallel_safe("no-such-tool")


def test_parallel_bash_is_a_profile_switch() -> None:
    profile = PermissionProfile.from_dict(
        {"shell": "unrestricted", "file_write": "full", "parallel_bash": True}
    )
    registry = ToolFactory(profile).create_registry(working_dir="/tmp")
    assert registry.is_parallel_safe("bash")
    assert profile.to_dict()["parallel_bash"] is True
