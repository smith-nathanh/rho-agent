from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from rho_agent.client.model import StreamEvent, ToolCall
from rho_agent.core.agent import Agent, ApprovalInterrupt
from rho_agent.core.session import Session
from rho_agent.runtime.options import RuntimeOptions
from rho_agent.runtime.run import run_prompt, run_prompt_stored
from rho_agent.runtime.store import SqliteRunStore
from rho_agent.runtime.types import LocalRuntime, RunState, ToolApprovalItem
from rho_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput
from rho_agent.tools.registry import ToolRegistry


class ScriptedClient:
    def __init__(self, scripts: list[list[StreamEvent]]) -> None:
        self._scripts = scripts
        self._index = 0

    async def stream(self, prompt: object) -> AsyncIterator[StreamEvent]:
        del prompt
        script = self._scripts[self._index]
        self._index += 1
        for event in script:
            yield event


class ApprovalTool(ToolHandler):
    @property
    def name(self) -> str:
        return "needs_approval"

    @property
    def description(self) -> str:
        return "Tool that requires approval."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"cmd": {"type": "string"}}}

    @property
    def requires_approval(self) -> bool:
        return True

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        cmd = invocation.arguments.get("cmd", "")
        return ToolOutput(content=f"executed:{cmd}")


def _build_runtime(client: ScriptedClient) -> LocalRuntime:
    session = Session(system_prompt="system")
    registry = ToolRegistry()
    registry.register(ApprovalTool())

    async def interrupt_approval(_: str, __: dict[str, Any]) -> bool:
        raise ApprovalInterrupt()

    agent = Agent(
        session=session,
        registry=registry,
        client=client,  # type: ignore[arg-type]
        approval_callback=interrupt_approval,
    )
    return LocalRuntime(
        agent=agent,
        session=session,
        registry=registry,
        model="gpt-5-mini",
        profile_name="readonly",
        session_id="session-1",
        options=RuntimeOptions(),
    )


@pytest.mark.asyncio
async def test_run_prompt_interruption_can_resume_from_state() -> None:
    client = ScriptedClient(
        scripts=[
            [
                StreamEvent(
                    type="tool_call",
                    tool_call=ToolCall(
                        id="tool-1",
                        name="needs_approval",
                        arguments={"cmd": "ls"},
                    ),
                ),
                StreamEvent(type="done", usage={"input_tokens": 10, "output_tokens": 5}),
            ],
            [
                StreamEvent(type="text", content="all set"),
                StreamEvent(type="done", usage={"input_tokens": 4, "output_tokens": 3}),
            ],
        ]
    )
    runtime = _build_runtime(client)

    interrupted = await run_prompt(runtime, "run command")
    assert interrupted.status == "interrupted"
    assert interrupted.state is not None
    assert [item.tool_call_id for item in interrupted.interruptions] == ["tool-1"]
    assert interrupted.state.pending_approvals[0].tool_name == "needs_approval"

    resumed = await run_prompt(
        runtime,
        interrupted.state,
        approval_decisions={"tool-1": True},
    )
    assert resumed.status == "completed"
    assert resumed.text == "all set"
    assert resumed.interruptions == []
    assert runtime.options.session_id == "session-1"
    assert any(
        message.get("role") == "tool" and message.get("content") == "executed:ls"
        for message in runtime.session.history
    )


@pytest.mark.asyncio
async def test_run_prompt_resume_denied_approval_halts_action_set() -> None:
    client = ScriptedClient(
        scripts=[
            [
                StreamEvent(
                    type="tool_call",
                    tool_call=ToolCall(
                        id="tool-1",
                        name="needs_approval",
                        arguments={"cmd": "rm a.txt"},
                    ),
                ),
                StreamEvent(
                    type="tool_call",
                    tool_call=ToolCall(
                        id="tool-2",
                        name="needs_approval",
                        arguments={"cmd": "rm b.txt"},
                    ),
                ),
                StreamEvent(type="done", usage={"input_tokens": 10, "output_tokens": 5}),
            ]
        ]
    )
    runtime = _build_runtime(client)

    interrupted = await run_prompt(runtime, "run command")
    assert interrupted.status == "interrupted"
    assert interrupted.state is not None
    assert [item.tool_call_id for item in interrupted.interruptions] == ["tool-1", "tool-2"]

    resumed = await run_prompt(
        runtime,
        interrupted.state,
        approval_decisions={"tool-1": False},
    )
    assert resumed.status == "completed"
    assert resumed.interruptions == []
    assert runtime.options.session_id == "session-1"

    tool_messages = [
        message for message in runtime.session.history if message.get("role") == "tool"
    ]
    assert len(tool_messages) == 2
    assert tool_messages[0]["tool_call_id"] == "tool-1"
    assert tool_messages[0]["content"] == "Command rejected by user. Awaiting new instructions."
    assert tool_messages[1]["tool_call_id"] == "tool-2"
    assert tool_messages[1]["content"] == "Command skipped - user rejected previous command."
    assert not any(message.get("content") == "executed:rm a.txt" for message in tool_messages)
    assert not any(message.get("content") == "executed:rm b.txt" for message in tool_messages)
    assert any(
        event.type == "tool_blocked" and event.tool_call_id == "tool-1" for event in resumed.events
    )


@pytest.mark.asyncio
async def test_run_prompt_stored_resume_from_persisted_store(tmp_path) -> None:
    run_id = "run-1"
    store = SqliteRunStore(tmp_path / "run-state.db")

    first_runtime = _build_runtime(
        ScriptedClient(
            scripts=[
                [
                    StreamEvent(
                        type="tool_call",
                        tool_call=ToolCall(
                            id="tool-1",
                            name="needs_approval",
                            arguments={"cmd": "ls"},
                        ),
                    ),
                    StreamEvent(type="done", usage={"input_tokens": 10, "output_tokens": 5}),
                ]
            ]
        )
    )
    interrupted = await run_prompt_stored(
        first_runtime,
        "run command",
        run_store=store,
        run_id=run_id,
    )
    assert interrupted.status == "interrupted"
    persisted = store.load(run_id)
    assert persisted is not None
    assert [item.tool_call_id for item in persisted.pending_approvals] == ["tool-1"]

    second_runtime = _build_runtime(
        ScriptedClient(
            scripts=[
                [
                    StreamEvent(type="text", content="all set"),
                    StreamEvent(type="done", usage={"input_tokens": 4, "output_tokens": 3}),
                ]
            ]
        )
    )
    resumed = await run_prompt_stored(
        second_runtime,
        None,
        approval_decisions={"tool-1": True},
        run_store=store,
        run_id=run_id,
    )
    assert resumed.status == "completed"
    assert resumed.text == "all set"
    assert store.load(run_id) is None
    assert any(
        message.get("role") == "tool" and message.get("content") == "executed:ls"
        for message in second_runtime.session.history
    )


@pytest.mark.asyncio
async def test_run_prompt_stored_none_without_persisted_state_raises(tmp_path) -> None:
    runtime = _build_runtime(ScriptedClient(scripts=[]))
    store = SqliteRunStore(tmp_path / "run-state.db")

    with pytest.raises(ValueError, match="No persisted run state found"):
        await run_prompt_stored(runtime, None, run_store=store, run_id="missing")


def test_sqlite_run_store_round_trip(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "run-state.db")
    state = RunState(
        session_id="session-1",
        system_prompt="system",
        history=[{"role": "user", "content": "hello"}],
        total_input_tokens=7,
        total_output_tokens=3,
        pending_approvals=[
            ToolApprovalItem(tool_call_id="t1", tool_name="bash", tool_args={"command": "pwd"})
        ],
    )

    store.save("run-1", state)
    loaded = store.load("run-1")
    assert loaded is not None
    assert loaded.session_id == "session-1"
    assert loaded.pending_approvals[0].tool_name == "bash"

    store.delete("run-1")
    assert store.load("run-1") is None
