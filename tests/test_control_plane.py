"""Control plane: cancel requests, session liveness and the session registry."""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from rho_agent.client.model import StreamEvent, ToolCall
from rho_agent.core.agent import Agent
from rho_agent.core.approvals import ApprovalQueue, pending_requests, respond
from rho_agent.core.config import AgentConfig
from rho_agent.core.events import ApprovalInterrupt
from rho_agent.core.session import Session
from rho_agent.core.session_meta import read_meta, update_meta
from rho_agent.core.session_store import SessionStore
from rho_agent.core.state import State
from rho_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput
from rho_agent.tools.handlers._child import child_session_kwargs, link_child
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


# --- approval queue ---


def _risky_call() -> list[StreamEvent]:
    return [
        StreamEvent(type="tool_call", tool_call=ToolCall(id="tc_1", name="risky", arguments={})),
        StreamEvent(type="done", usage={"input_tokens": 10, "output_tokens": 5}),
    ]


def _risky_session(tmp_path: Path):
    registry = ToolRegistry()
    registry.register(_NeedsApproval())
    store, session = _session(tmp_path, _risky_call(), _text("finished"), registry=registry)
    session.approval_callback = ApprovalQueue(session, poll_seconds=0.01)
    return store, session


async def _answer_when_pending(store: SessionStore, approved: bool) -> str:
    """Play the monitor: wait for a request, check the status, answer it."""
    for _ in range(500):
        pending = store.pending_approvals()
        if pending:
            info, request = pending[0]
            assert info.status == "waiting_approval"
            respond(request, approved)
            return request.id
        await asyncio.sleep(0.01)
    raise AssertionError("no approval request was filed")


@pytest.mark.asyncio
async def test_queued_approval_is_answered_from_another_task(tmp_path: Path) -> None:
    store, session = _risky_session(tmp_path)

    result, request_id = await asyncio.gather(
        session.run("do the risky thing"), _answer_when_pending(store, approved=True)
    )

    assert result.status == "completed"
    assert [e.tool_name for e in result.events if e.type == "tool_end"] == ["risky"]
    session_dir = store.base_dir / session.id
    assert pending_requests(session_dir) == []
    assert list((session_dir / "approvals").iterdir()) == []
    trace = (session_dir / "trace.jsonl").read_text()
    assert f'"type": "approval_requested", "request_id": "{request_id}"' in trace
    assert '"outcome": "approved"' in trace


@pytest.mark.asyncio
async def test_denied_queued_approval_blocks_the_tool(tmp_path: Path) -> None:
    store, session = _risky_session(tmp_path)

    result, _ = await asyncio.gather(
        session.run("do the risky thing"), _answer_when_pending(store, approved=False)
    )

    assert [e.type for e in result.events if e.type in ("tool_end", "tool_blocked")] == [
        "tool_blocked"
    ]
    assert '"outcome": "denied"' in (store.base_dir / session.id / "trace.jsonl").read_text()


@pytest.mark.asyncio
async def test_cancel_ends_a_wait_for_approval(tmp_path: Path) -> None:
    store, session = _session(tmp_path)
    queue = ApprovalQueue(session, poll_seconds=0.01)

    waiting = asyncio.ensure_future(queue("risky", {}))
    await asyncio.sleep(0.05)
    assert len(pending_requests(store.base_dir / session.id)) == 1
    session.cancel()

    assert await asyncio.wait_for(waiting, timeout=2) is False
    assert pending_requests(store.base_dir / session.id) == []


def test_child_session_files_approvals_under_its_own_id(tmp_path: Path) -> None:
    _, parent = _session(tmp_path)
    parent.approval_callback = ApprovalQueue(parent)
    kwargs = child_session_kwargs(parent, State(), share_client=True)
    child = Session(_agent(tmp_path), **kwargs)

    link_child(parent, child, "delegate", "do part of it")

    assert isinstance(child.approval_callback, ApprovalQueue)
    assert child.approval_callback._session is child


@pytest.mark.asyncio
async def test_pending_approvals_skip_dead_sessions(tmp_path: Path) -> None:
    store, session = _session(tmp_path)
    queue = ApprovalQueue(session, poll_seconds=0.01)
    waiting = asyncio.ensure_future(queue("risky", {}))
    await asyncio.sleep(0.05)
    assert len(store.pending_approvals()) == 1

    update_meta(store.base_dir / session.id, pid=_dead_pid())
    assert store.pending_approvals() == []

    session.cancel()
    await waiting


@pytest.mark.asyncio
async def test_interrupted_approval_is_not_recorded_as_completed(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(_NeedsApproval())
    store, session = _session(tmp_path, _risky_call(), registry=registry)

    async def interrupt(tool_name: str, tool_args: dict[str, Any]) -> bool:
        raise ApprovalInterrupt("ask someone else")

    session.approval_callback = interrupt
    result = await session.run("do the risky thing")

    assert result.status == "interrupted"
    assert read_meta(store.base_dir / session.id)["status"] == "interrupted"


# --- targeting children, ID collisions ---


def test_find_matches_child_sessions(tmp_path: Path) -> None:
    store, parent = _session(tmp_path)
    child_id = child_session_kwargs(parent, State(), share_client=True)["session_id"]

    assert [info.id for info in store.find(child_id[:6])] == [child_id]
    assert [info.id for info in store.find("all")] == [parent.id]


def test_generated_session_id_never_reuses_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uuid

    store, first = _session(tmp_path)
    ids = iter([uuid.UUID(hex=first.id + "0" * 24), uuid.UUID(hex="ab" * 16)])
    monkeypatch.setattr(uuid, "uuid4", lambda: next(ids))

    second = store.create_session(_agent(tmp_path))

    assert second.id == "abababab"
    assert read_meta(store.base_dir / first.id)["id"] == first.id


# --- monitor commands ---


def _monitor(store: SessionStore):
    from rho_agent.cli import monitor

    return monitor, monitor.MonitorSession(store.base_dir)


@pytest.mark.asyncio
async def test_monitor_lists_and_approves_requests(tmp_path: Path) -> None:
    store, session = _session(tmp_path)
    queue = ApprovalQueue(session, poll_seconds=0.01)
    waiting = asyncio.ensure_future(queue("risky", {"path": "/etc/hosts"}))
    await asyncio.sleep(0.05)
    request_id = pending_requests(store.base_dir / session.id)[0].id
    module, mon = _monitor(store)

    with module.console.capture() as capture:
        mon._cmd_approvals()
        mon._cmd_answer(request_id[:3], approved=True)

    output = capture.get()
    assert request_id in output
    assert "risky" in output
    assert "approved" in output
    assert await asyncio.wait_for(waiting, timeout=2) is True


@pytest.mark.asyncio
async def test_watch_shows_only_the_tail_by_default(tmp_path: Path) -> None:
    store, session = _session(tmp_path, _text("first answer"), _text("second answer"))
    await session.run("one")
    await session.run("two")
    module, mon = _monitor(store)

    with module.console.capture() as capture:
        mon._cmd_watch(session.id, "3")

    output = capture.get()
    assert "second answer" in output
    assert "first answer" not in output
    assert "earlier events" in output


def test_directive_to_a_finished_session_says_when_it_will_be_read(tmp_path: Path) -> None:
    store, session = _session(tmp_path)
    update_meta(store.base_dir / session.id, status="completed")
    module, mon = _monitor(store)

    with module.console.capture() as capture:
        mon._cmd_directive(session.id, "look at the billing module")

    assert "not running" in capture.get()
    assert session._consume_directives() == ["look at the billing module"]


# --- interactive session ---


def _interactive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from rho_agent.cli.interactive import InteractiveSession

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")  # /resume builds a real client
    store, session = _session(tmp_path)
    repl = InteractiveSession(
        session=session,
        approval_handler=MagicMock(),
        mode_name="test",
        working_dir=str(tmp_path),
        session_store=store,
    )
    return store, session, repl


async def _idle_prompt(repl, act):
    """Sit at the prompt, let ``act(pipe)`` happen, and return what the prompt yields."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        prompt_session: PromptSession[str] = PromptSession(input=pipe, output=DummyOutput())
        waiting = asyncio.ensure_future(repl._next_input(prompt_session, poll_seconds=0.01))
        await asyncio.sleep(0.1)
        act(pipe)
        return await asyncio.wait_for(waiting, timeout=5)


@pytest.mark.asyncio
async def test_idle_prompt_returns_typed_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, repl = _interactive(tmp_path, monkeypatch)

    assert await _idle_prompt(repl, lambda pipe: pipe.send_text("hello\r")) == ("user", "hello")


@pytest.mark.asyncio
async def test_idle_prompt_picks_up_a_directive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session, repl = _interactive(tmp_path, monkeypatch)
    _, mon = _monitor(store)

    result = await _idle_prompt(repl, lambda pipe: mon._cmd_directive(session.id, "run the tests"))

    assert result == ("directive", "run the tests")


@pytest.mark.asyncio
async def test_idle_prompt_notices_an_external_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session, repl = _interactive(tmp_path, monkeypatch)
    cancel_file = store.base_dir / session.id / "cancel"

    assert await _idle_prompt(repl, lambda pipe: cancel_file.touch()) == ("cancel", "")
    assert not cancel_file.exists()


@pytest.mark.asyncio
async def test_resume_command_continues_as_the_resumed_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, fresh, repl = _interactive(tmp_path, monkeypatch)
    fresh_id = fresh.id
    _, earlier = _session(tmp_path, _text("an answer"))
    await earlier.run("an earlier question")

    repl._handle_resume(f"/resume {earlier.id}")

    assert repl.session.id == earlier.id
    assert repl.session._session_dir == store.base_dir / earlier.id
    assert len(repl.session.state.messages) == 2
    assert not (store.base_dir / fresh_id).exists()  # the unused session is dropped
    assert [info.id for info in store.list()] == [earlier.id]
