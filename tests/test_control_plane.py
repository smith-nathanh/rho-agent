"""Control plane: cancel requests, session liveness and the session registry."""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from rho_agent.client.model import StreamEvent, ToolCall
from rho_agent.core.agent import Agent
from rho_agent.core.config import AgentConfig
from rho_agent.core.session_meta import read_meta, update_meta
from rho_agent.core.session_store import SessionStore
from rho_agent.core.state import State
from rho_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput
from rho_agent.tools.handlers._child import child_session_kwargs
from rho_agent.tools.registry import ToolRegistry


class _Client:
    """Model client that replays one scripted turn per call."""

    def __init__(self, *turns: list[StreamEvent]) -> None:
        self._turns = list(turns)

    async def stream(self, prompt: Any):
        events = self._turns.pop(0) if self._turns else _text("done")
        for event in events:
            yield event


def _text(text: str, cost: float = 0.0) -> list[StreamEvent]:
    return [
        StreamEvent(type="text", content=text),
        StreamEvent(type="done", usage={"input_tokens": 10, "output_tokens": 5, "cost_usd": cost}),
    ]


def _agent(tmp_path: Path, registry: ToolRegistry | None = None) -> Agent:
    agent = MagicMock(spec=Agent)
    agent.config = AgentConfig(
        system_prompt="test", profile="readonly", model="test-model", working_dir=str(tmp_path)
    )
    agent.system_prompt = "test"
    agent.registry = registry or ToolRegistry()
    agent.create_client = MagicMock(return_value=_Client())
    return agent


def _session(tmp_path: Path, *turns: list[StreamEvent], registry: ToolRegistry | None = None):
    store = SessionStore(tmp_path / "sessions")
    session = store.create_session(_agent(tmp_path, registry))
    session._client = _Client(*turns)
    return store, session


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# --- cancel ---


@pytest.mark.asyncio
async def test_in_process_cancel_does_not_stop_the_next_run(tmp_path: Path) -> None:
    store, session = _session(tmp_path, _text("one"))
    session.cancel()

    result = await session.run("after a Ctrl+C on the previous turn")

    assert result.status == "completed"
    assert not (store.base_dir / session.id / "cancel").exists()


@pytest.mark.asyncio
async def test_cancel_file_stops_one_run_and_is_consumed(tmp_path: Path) -> None:
    store, session = _session(tmp_path, _text("two"))
    cancel_file = store.base_dir / session.id / "cancel"
    cancel_file.touch()

    first = await session.run("one")
    assert first.status == "cancelled"
    assert session.cancelled_externally is True
    assert not cancel_file.exists()

    second = await session.run("two")
    assert second.status == "completed"
    assert session.cancelled_externally is False


@pytest.mark.asyncio
async def test_request_cancel_skips_finished_sessions(tmp_path: Path) -> None:
    store, session = _session(tmp_path, _text("done"))
    await session.run("finish")
    _, live = _session(tmp_path)

    results = {info.id: store.request_cancel(info) for info in store.find("all")}

    assert results == {session.id: False, live.id: True}
    assert not (store.base_dir / session.id / "cancel").exists()
    assert (store.base_dir / live.id / "cancel").exists()


def test_resume_clears_a_stale_cancel_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")  # resume builds a real client
    store, session = _session(tmp_path)
    cancel_file = store.base_dir / session.id / "cancel"
    cancel_file.touch()

    store.resume(session.id, agent_config=AgentConfig(model="test-model"))

    assert not cancel_file.exists()


# --- liveness and status ---


def test_new_session_is_registered_with_owner_and_project(tmp_path: Path) -> None:
    store, session = _session(tmp_path)

    info = store.list()[0]

    assert info.id == session.id
    assert info.status == "created"
    assert info.cwd == str(tmp_path)
    assert info.host == socket.gethostname()
    assert info.pid is not None
    assert info.live


def test_session_whose_process_is_gone_is_dead(tmp_path: Path) -> None:
    store, session = _session(tmp_path)
    session_dir = store.base_dir / session.id
    update_meta(session_dir, status="running", pid=_dead_pid())

    info = store.list()[0]

    assert info.status == "dead"
    assert not info.live
    assert store.request_cancel(info) is False


def test_live_status_without_a_recorded_pid_is_unknown(tmp_path: Path) -> None:
    store, session = _session(tmp_path)
    session_dir = store.base_dir / session.id
    (session_dir / "meta.json").write_text('{"status": "running", "model": "m"}')

    info = store.list()[0]

    assert info.status == "unknown"
    assert store.request_cancel(info) is False


def test_liveness_is_not_guessed_for_another_host(tmp_path: Path) -> None:
    store, session = _session(tmp_path)
    update_meta(
        store.base_dir / session.id, status="running", pid=_dead_pid(), host="another-machine"
    )

    assert store.list()[0].status == "running"


@pytest.mark.asyncio
async def test_run_records_status_prompt_and_cost(tmp_path: Path) -> None:
    store, session = _session(tmp_path, _text("hello", cost=0.25))

    await session.run("summarise the repo")

    meta = read_meta(store.base_dir / session.id)
    assert meta["status"] == "completed"
    assert meta["first_prompt"] == "summarise the repo"
    assert meta["cost_usd"] == pytest.approx(0.25)
    assert meta["run_count"] == 1
    info = store.list()[0]
    assert info.first_prompt == "summarise the repo"
    assert info.cost_usd == pytest.approx(0.25)


class _NeedsApproval(ToolHandler):
    @property
    def name(self) -> str:
        return "risky"

    @property
    def description(self) -> str:
        return "Needs approval."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    @property
    def requires_approval(self) -> bool:
        return True

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        return ToolOutput(content="ok", success=True)


@pytest.mark.asyncio
async def test_status_is_waiting_approval_while_the_callback_runs(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(_NeedsApproval())
    call = [
        StreamEvent(type="tool_call", tool_call=ToolCall(id="tc_1", name="risky", arguments={})),
        StreamEvent(type="done", usage={"input_tokens": 10, "output_tokens": 5}),
    ]
    store, session = _session(tmp_path, call, _text("finished"), registry=registry)
    session_dir = store.base_dir / session.id
    seen: dict[str, Any] = {}

    async def approve(tool_name: str, tool_args: dict[str, Any]) -> bool:
        seen.update(read_meta(session_dir))
        return True

    session.approval_callback = approve
    result = await session.run("do the risky thing")

    assert result.status == "completed"
    assert seen["status"] == "waiting_approval"
    assert seen["waiting_on"] == "risky"
    meta = read_meta(session_dir)
    assert meta["status"] == "completed"
    assert meta["waiting_on"] is None


# --- children and listing ---


def test_children_are_listed_under_their_parent(tmp_path: Path) -> None:
    store, parent = _session(tmp_path)
    child_config = AgentConfig(model="child-model", profile="readonly")

    kwargs = child_session_kwargs(parent, State(), share_client=True, config=child_config)

    info = store.list()[0]
    assert [child.id for child in info.children] == [kwargs["session_id"]]
    child = info.children[0]
    assert child.model == "child-model"
    assert child.parent_id == parent.id
    assert child.cwd == str(tmp_path)


def test_list_orders_by_last_activity(tmp_path: Path) -> None:
    store, older = _session(tmp_path)
    _, newer = _session(tmp_path)
    update_meta(store.base_dir / older.id, last_activity="2999-01-01T00:00:00+00:00")

    assert [info.id for info in store.list()] == [older.id, newer.id]


def test_print_sessions_groups_by_project(tmp_path: Path) -> None:
    from rho_agent.cli import session_table

    store, parent = _session(tmp_path)
    child_session_kwargs(parent, State(), share_client=True)

    with session_table.console.capture() as capture:
        session_table.print_sessions(store.list())

    output = capture.get()
    assert parent.id in output
    assert "└" in output
    assert "created" in output
