"""Tests for child agent sessions spawned by the delegate tool."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from rho_agent.client.model import StreamEvent, ToolCall
from rho_agent.core.agent import Agent
from rho_agent.core.config import AgentConfig
from rho_agent.core.session import Session
from rho_agent.core.state import State
from rho_agent.tools.factory import build_registry
from rho_agent.tools.handlers._child import link_child


class _Client:
    """Parent turns are scripted; any call whose last message is a child
    instruction gets the child reply."""

    def __init__(self, parent_turns: list[list[StreamEvent]], child_delay: float = 0.0) -> None:
        self._parent_turns = list(parent_turns)
        self._child_delay = child_delay
        self.child_prompts: list[Any] = []

    async def stream(self, prompt: Any):
        last = prompt.messages[-1]
        if last.role == "user" and str(last.content).startswith("CHILD:"):
            self.child_prompts.append(prompt)
            await asyncio.sleep(self._child_delay)
            yield StreamEvent(type="text", content=f"answer to {last.content}")
            yield StreamEvent(
                type="done", usage={"input_tokens": 100, "output_tokens": 10, "cost_usd": 0.2}
            )
            return
        for e in self._parent_turns.pop(0):
            yield e


def _delegate(call_id: str, instruction: str, **extra: Any) -> StreamEvent:
    return StreamEvent(
        type="tool_call",
        tool_call=ToolCall(
            id=call_id, name="delegate", arguments={"instruction": instruction, **extra}
        ),
    )


def _done(cost: float = 0.1) -> StreamEvent:
    return StreamEvent(
        type="done", usage={"input_tokens": 10, "output_tokens": 1, "cost_usd": cost}
    )


def _parent(client: _Client, tmp_path: Path) -> Session:
    agent = Agent(AgentConfig(system_prompt="parent system", access="full", model="m"))
    session_dir = tmp_path / "parent"
    session_dir.mkdir()
    return Session(
        agent,
        client=client,
        state=State(trace_path=session_dir / "trace.jsonl"),
        session_dir=session_dir,
    )


def test_factory_registers_delegate_for_python_api_users() -> None:
    registry = build_registry("full", working_dir="/tmp")
    assert "delegate" in registry
    assert registry.is_parallel_safe("delegate")

    assert "delegate" not in build_registry("full", working_dir="/tmp", tools=["bash"])


async def test_delegate_runs_child_with_trace_and_rolls_up_usage(tmp_path: Path) -> None:
    client = _Client(
        [
            [_delegate("d1", "CHILD: count the files"), _done()],
            [StreamEvent(type="text", content="final"), _done()],
        ]
    )
    parent = _parent(client, tmp_path)
    assert "delegate" in parent.registry

    result = await parent.run("go")

    assert result.status == "completed"
    tool_msg = next(m for m in parent.state.messages if m["role"] == "tool")
    assert tool_msg["content"] == "answer to CHILD: count the files"

    # The child had the parent's identity but could not delegate further.
    child_prompt = client.child_prompts[0]
    assert child_prompt.system == "parent system"
    assert "delegate" not in [t["function"]["name"] for t in child_prompt.tools]
    assert len(child_prompt.messages) == 1

    # Its trace sits under the parent's session directory.
    traces = list((tmp_path / "parent" / "children").glob("*/trace.jsonl"))
    assert len(traces) == 1
    child_state = State.from_jsonl(traces[0].read_bytes())
    assert child_state.messages[-1]["content"] == "answer to CHILD: count the files"

    # Parent totals include the child, and survive a resume.
    assert abs(parent.state.usage["cost_usd"] - 0.4) < 1e-9
    assert parent.state.usage["input_tokens"] == 120
    restored = State.from_jsonl((tmp_path / "parent" / "trace.jsonl").read_bytes())
    assert restored.usage == parent.state.usage


async def test_delegates_in_one_turn_run_in_parallel(tmp_path: Path) -> None:
    client = _Client(
        [
            [_delegate("d1", "CHILD: one"), _delegate("d2", "CHILD: two"), _done()],
            [StreamEvent(type="text", content="final"), _done()],
        ],
        child_delay=0.3,
    )
    parent = _parent(client, tmp_path)

    start = time.perf_counter()
    result = await parent.run("go")
    elapsed = time.perf_counter() - start

    assert result.status == "completed"
    assert elapsed < 0.55  # serial would be 0.6s
    results = [m["content"] for m in parent.state.messages if m["role"] == "tool"]
    assert results == ["answer to CHILD: one", "answer to CHILD: two"]
    assert len(list((tmp_path / "parent" / "children").iterdir())) == 2


async def test_full_context_child_gets_a_valid_history(tmp_path: Path) -> None:
    client = _Client(
        [
            [_delegate("d1", "CHILD: continue", full_context=True), _done()],
            [StreamEvent(type="text", content="final"), _done()],
        ]
    )
    parent = _parent(client, tmp_path)

    await parent.run("the parent task")

    messages = client.child_prompts[0].messages
    assert messages[0].content == "the parent task"
    # The parent's unanswered delegate call was given a placeholder result.
    assert messages[1].tool_calls and messages[2].role == "tool"
    assert messages[-1].content == "CHILD: continue"


def test_child_inherits_remaining_budget_and_controls(tmp_path: Path) -> None:
    client = _Client([])
    parent = _parent(client, tmp_path)
    parent.max_cost_usd = 1.0
    parent.max_wall_seconds = 100.0
    parent._wall_start = time.monotonic() - 40
    parent.state.update_usage(cost_usd=0.3)

    async def approve(name: str, args: dict[str, Any]) -> bool:
        return True

    parent.approval_callback = approve
    child = Session(Agent(AgentConfig(system_prompt="s", model="m")), client=client)

    link_child(parent, child, "delegate", "do it")

    assert abs(child.max_cost_usd - 0.7) < 1e-9
    assert 55 < child.max_wall_seconds <= 60
    assert child.approval_callback is approve

    assert child._is_cancelled() is False
    parent.cancel()
    assert child._is_cancelled() is True


def test_pausing_the_parent_pauses_the_child(tmp_path: Path) -> None:
    client = _Client([])
    parent = _parent(client, tmp_path)
    child = Session(Agent(AgentConfig(system_prompt="s", model="m")), client=client)
    child.parent = parent

    assert child._pause_requested() is False
    (tmp_path / "parent" / "pause").touch()
    assert child._pause_requested() is True
