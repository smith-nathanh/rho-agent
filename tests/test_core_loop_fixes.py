"""Tests for resume, compaction, retries, caps and tool-call pairing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from rho_agent.cli.main_cmd import _default_prompt_for, _resume_config
from rho_agent.client.model import (
    ModelCallError,
    StreamEvent,
    ToolCall,
    classify_error,
    parse_tool_arguments,
)
from rho_agent.core.agent import Agent
from rho_agent.core.config import AgentConfig
from rho_agent.core.models import compact_threshold
from rho_agent.core.session import Session
from rho_agent.core.session_store import SessionStore
from rho_agent.core.state import State
from rho_agent.eval.harbor.trajectory import TrajectoryBuilder
from rho_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput
from rho_agent.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _EchoHandler(ToolHandler):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echo tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"text": {"type": "string"}}}

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        self.calls.append(invocation.arguments)
        return ToolOutput(content=invocation.arguments.get("text", ""))


class _SlowHandler(_EchoHandler):
    @property
    def name(self) -> str:
        return "slow"

    async def handle(self, invocation: ToolInvocation) -> ToolOutput:
        await asyncio.sleep(30)
        return ToolOutput(content="finished")


class _ScriptedClient:
    """Model client that replays one scripted list of StreamEvents per call."""

    def __init__(self, *calls: list[StreamEvent], summary: str | Exception = "SUMMARY") -> None:
        self._calls = list(calls)
        self.prompts: list[Any] = []
        self.summary = summary
        self.summary_prompts: list[Any] = []

    async def stream(self, prompt: Any):
        self.prompts.append(prompt)
        events = self._calls.pop(0) if self._calls else [_text("done"), _done()]
        for e in events:
            yield e

    async def complete_prompt(self, prompt: Any) -> tuple[str, dict[str, Any]]:
        self.summary_prompts.append(prompt)
        if isinstance(self.summary, Exception):
            raise self.summary
        return self.summary, {"input_tokens": 50, "output_tokens": 5, "cost_usd": 0.01}


def _text(content: str) -> StreamEvent:
    return StreamEvent(type="text", content=content)


def _done(input_tokens: int = 10, cost: float = 0.0) -> StreamEvent:
    return StreamEvent(
        type="done",
        usage={"input_tokens": input_tokens, "output_tokens": 2, "cost_usd": cost},
    )


def _call(call_id: str, name: str = "echo", **arguments: Any) -> StreamEvent:
    return StreamEvent(
        type="tool_call", tool_call=ToolCall(id=call_id, name=name, arguments=arguments)
    )


def _error(kind: str | None, content: str = "boom") -> StreamEvent:
    return StreamEvent(type="error", content=content, error_kind=kind)


def _session(
    client: _ScriptedClient,
    *handlers: ToolHandler,
    trace_path: Path | None = None,
    model: str = "test-model",
) -> Session:
    registry = ToolRegistry()
    for h in handlers:
        registry.register(h)
    agent = MagicMock(spec=Agent)
    agent.config = AgentConfig(system_prompt="sys", profile="readonly", model=model)
    agent.system_prompt = "sys"
    agent.registry = registry
    agent.sandbox_manager = None
    session = Session(agent, client=client, state=State(trace_path=trace_path))
    session.retry_backoff_seconds = 0
    return session


def _assert_tool_calls_paired(messages: list[dict[str, Any]]) -> None:
    """Every assistant tool call is followed directly by its results."""
    i = 0
    while i < len(messages):
        calls = messages[i].get("tool_calls") or []
        i += 1
        expected = [tc["id"] for tc in calls]
        got = []
        while i < len(messages) and messages[i].get("role") == "tool":
            got.append(messages[i]["tool_call_id"])
            i += 1
        assert sorted(got) == sorted(expected)


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


async def test_resume_after_compaction_restores_compacted_history(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    client = _ScriptedClient(
        [_call("a", text="x" * 400), _done()],
        [_call("b", text="y" * 400), _done()],
        [_text("answer"), _done()],
    )
    session = _session(client, _EchoHandler(), trace_path=trace)
    await session.run("the original task")
    session.compact_at_tokens = 1
    result = await session.compact()
    assert result.error is None
    assert result.tokens_after < result.tokens_before

    restored = State.from_jsonl(trace.read_bytes())
    assert restored.messages == session.state.messages
    assert restored.messages[0] == {"role": "user", "content": "the original task"}
    assert any("SUMMARY" in str(m.get("content")) for m in restored.messages)


async def test_resume_rebuilds_usage_and_run_count(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    client = _ScriptedClient(
        [_text("one"), _done(input_tokens=100, cost=0.25)],
        [_text("two"), _done(input_tokens=200, cost=0.5)],
    )
    session = _session(client, trace_path=trace)
    await session.run("first")
    await session.run("second")

    restored = State.from_jsonl(trace.read_bytes())
    assert restored.run_count == 2
    assert restored.status == "completed"
    assert restored.usage["input_tokens"] == 300
    assert restored.usage["cost_usd"] == 0.75
    assert restored.usage == session.state.usage


async def test_resume_with_model_override_keeps_the_saved_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RHO_AGENT_API_KEY", "test-key")  # resume builds a real client
    store = SessionStore(tmp_path / "sessions")
    saved = AgentConfig(
        system_prompt="You are the saved agent.",
        model="saved-model",
        profile="developer",
        working_dir=str(tmp_path),
        base_url="http://localhost:9/v1",
        reasoning_effort="high",
        project_instructions=False,
        skills=False,
    )
    original = store.create_session(Agent(saved))
    original._client = _ScriptedClient([_text("one"), _done()])
    await original.run("first")

    config, prompt_kept = _resume_config(store.load_config(original.id), {"model": "other-model"})
    assert not prompt_kept
    resumed = store.resume(original.id, agent_config=config)
    client = _ScriptedClient([_text("two"), _done()])
    resumed._client = client
    result = await resumed.run("second")

    assert result.status == "completed"
    config = resumed.agent.config
    assert config.model == "other-model"
    assert (config.profile, config.working_dir) == ("developer", str(tmp_path))
    assert (config.base_url, config.reasoning_effort) == ("http://localhost:9/v1", "high")
    assert client.prompts[0].system == "You are the saved agent."
    assert [m["content"] for m in resumed.state.messages if m["role"] == "user"] == [
        "first",
        "second",
    ]
    # The override lasts for this resume only.
    assert AgentConfig.from_file(store.base_dir / original.id / "config.yaml").model == (
        "saved-model"
    )


def test_resume_flags_override_the_saved_config(tmp_path: Path) -> None:
    saved = AgentConfig(
        system_prompt="Custom prompt.",
        model="saved-model",
        profile="readonly",
        working_dir=str(tmp_path),
        reasoning_effort="low",
    )
    overrides = {
        "reasoning_effort": "high",
        "base_url": "http://localhost:9/v1",
        "profile": "developer",
    }

    config, prompt_kept = _resume_config(saved, overrides)

    assert (config.reasoning_effort, config.base_url) == ("high", "http://localhost:9/v1")
    assert (config.profile, config.model) == ("developer", "saved-model")
    assert config.system_prompt == "Custom prompt."
    assert prompt_kept  # a custom prompt may describe the old profile
    assert saved.profile == "readonly"

    config, prompt_kept = _resume_config(saved, {"profile": "developer"}, system_prompt="New.")
    assert (config.system_prompt, prompt_kept) == ("New.", False)


def test_resume_renders_the_default_prompt_again_for_a_new_profile_or_dir(
    tmp_path: Path,
) -> None:
    old_dir, new_dir = tmp_path / "old", tmp_path / "new"
    saved = AgentConfig(profile="readonly", working_dir=str(old_dir))
    saved.system_prompt = _default_prompt_for(saved)
    assert str(old_dir) in saved.system_prompt

    config, prompt_kept = _resume_config(
        saved, {"profile": "developer", "working_dir": str(new_dir)}
    )

    assert not prompt_kept
    assert config.system_prompt == _default_prompt_for(config)
    assert str(new_dir) in config.system_prompt
    assert str(old_dir) not in config.system_prompt

    # Flags that the prompt does not describe leave it alone.
    config, prompt_kept = _resume_config(saved, {"reasoning_effort": "high"})
    assert (config.system_prompt, prompt_kept) == (saved.system_prompt, False)


def test_resume_cli_applies_flags_given_on_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from rho_agent.cli import main_cmd

    monkeypatch.setenv("RHO_AGENT_API_KEY", "test-key")
    monkeypatch.setattr(main_cmd, "SESSIONS_DIR", tmp_path / "sessions")
    store = SessionStore(tmp_path / "sessions")
    saved = AgentConfig(
        system_prompt="Custom prompt.",
        model="saved-model",
        profile="readonly",
        working_dir=str(tmp_path),
        base_url="http://localhost:9/v1",
    )
    session_id = store.create_session(Agent(saved)).id
    seen: dict[str, Any] = {}

    async def fake_run_single(session: Session, prompt: str, **_: Any) -> None:
        seen["config"] = session.agent.config
        seen["system"] = session.agent.system_prompt
        seen["prompt"] = prompt

    monkeypatch.setattr(main_cmd, "run_single", fake_run_single)
    new_dir = tmp_path / "elsewhere"
    new_dir.mkdir()
    args = ["main", "-r", session_id, "--reasoning-effort", "high", "--profile", "developer"]
    args += ["--working-dir", str(new_dir), "go on"]

    result = CliRunner().invoke(main_cmd.app, args)

    assert result.exit_code == 0, result.output
    config = seen["config"]
    assert (config.reasoning_effort, config.profile) == ("high", "developer")
    assert config.working_dir == str(new_dir.resolve())
    # Not given on the command line, so kept from the saved session.
    assert (config.model, config.base_url) == ("saved-model", "http://localhost:9/v1")
    assert seen["system"].startswith("Custom prompt.")
    assert seen["prompt"] == "go on"
    assert "custom system prompt" in result.output


async def test_unresolvable_system_prompt_leaves_the_trace_untouched(tmp_path: Path) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text(
        "---\nvariables:\n  platform:\n    required: true\n---\nRunning on {{ platform }}.\n"
    )
    trace = tmp_path / "trace.jsonl"
    agent = Agent(AgentConfig(system_prompt=str(prompt_file), model="test-model"))
    session = Session(agent, client=_ScriptedClient(), state=State(trace_path=trace))

    with pytest.raises(ValueError, match="Missing required variable: platform"):
        await session.run("hello")

    assert session.state.messages == []
    assert session.state.run_count == 0
    assert not trace.exists() or trace.read_text() == ""


def test_resume_of_killed_run_closes_dangling_tool_calls() -> None:
    lines = [
        {"type": "run_start", "prompt": "go"},
        {"type": "message", "role": "user", "content": "go"},
        {
            "type": "message",
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "echo", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "echo", "arguments": "{}"}},
            ],
        },
        {"type": "message", "role": "tool", "tool_call_id": "a", "content": "ok"},
    ]
    data = ("\n".join(json.dumps(line) for line in lines) + '\n{"type": "mess').encode()

    restored = State.from_jsonl(data)

    assert restored.status == "interrupted"
    assert restored.pending_tool_call_ids() == []
    _assert_tool_calls_paired(restored.messages)
    assert restored.messages[-1]["tool_call_id"] == "b"


def test_dangling_call_in_mid_history_is_repaired_in_place() -> None:
    state = State()
    state.add_user_message("go")
    state.add_assistant_tool_calls(
        [{"id": "a", "type": "function", "function": {"name": "echo", "arguments": "{}"}}]
    )
    state.add_user_message("later message")

    assert state.close_pending_tool_calls("interrupted") == 1
    assert [m["role"] for m in state.messages] == ["user", "assistant", "tool", "user"]
    _assert_tool_calls_paired(state.messages)


# ---------------------------------------------------------------------------
# Tool-call handling
# ---------------------------------------------------------------------------


async def test_cancel_mid_batch_leaves_no_dangling_tool_calls(tmp_path: Path) -> None:
    client = _ScriptedClient([_call("a", text="1"), _call("b", text="2"), _done()])
    handler = _EchoHandler()
    session = _session(client, handler, trace_path=tmp_path / "trace.jsonl")

    def cancel_after_first(event: Any) -> None:
        if event.type == "tool_end":
            session._cancelled = True

    result = await session.run("go", on_event=cancel_after_first)

    assert result.status == "cancelled"
    assert len(handler.calls) == 1
    _assert_tool_calls_paired(session.state.messages)
    restored = State.from_jsonl((tmp_path / "trace.jsonl").read_bytes())
    assert restored.messages == session.state.messages


async def test_text_alongside_tool_calls_is_kept() -> None:
    client = _ScriptedClient(
        [_text("Checking the file. "), _call("a", text="hi"), _done()],
        [_text("done"), _done()],
    )
    session = _session(client, _EchoHandler())
    await session.run("go")

    assistant = session.state.messages[1]
    assert assistant["tool_calls"][0]["id"] == "a"
    assert assistant["content"] == "Checking the file. "


async def test_malformed_arguments_are_reported_not_executed() -> None:
    args, parse_error = parse_tool_arguments('{"text": "unterminated')
    assert args == {} and parse_error
    bad = StreamEvent(
        type="tool_call",
        tool_call=ToolCall(id="a", name="echo", arguments=args, parse_error=parse_error),
    )
    client = _ScriptedClient([bad, _done()], [_text("ok"), _done()])
    handler = _EchoHandler()
    session = _session(client, handler)

    result = await session.run("go")

    assert result.status == "completed"
    assert handler.calls == []
    tool_result = session.state.messages[2]
    assert tool_result["role"] == "tool"
    assert "not valid JSON" in tool_result["content"]


def test_parse_tool_arguments_accepts_only_objects() -> None:
    assert parse_tool_arguments("") == ({}, None)
    assert parse_tool_arguments('{"a": 1}') == ({"a": 1}, None)
    assert parse_tool_arguments("[1, 2]")[1] is not None


# ---------------------------------------------------------------------------
# Retries and limits
# ---------------------------------------------------------------------------


async def test_transient_error_is_retried_and_partial_text_dropped() -> None:
    client = _ScriptedClient(
        [_text("partial "), _error("transient")],
        [_text("full answer"), _done()],
    )
    session = _session(client)

    result = await session.run("go")

    assert result.status == "completed"
    assert result.text == "full answer"
    assert len(client.prompts) == 2
    assert session.state.messages[-1] == {"role": "assistant", "content": "full answer"}


async def test_transient_error_gives_up_after_max_retries() -> None:
    client = _ScriptedClient(*[[_error("transient")] for _ in range(10)])
    session = _session(client)
    session.max_model_retries = 2

    result = await session.run("go")

    assert result.status == "error"
    assert len(client.prompts) == 3


async def test_fatal_and_unclassified_errors_are_not_retried() -> None:
    for kind in ("fatal", None):
        client = _ScriptedClient([_error(kind)], [_text("never"), _done()])
        session = _session(client)
        result = await session.run("go")
        assert result.status == "error"
        assert len(client.prompts) == 1


async def test_context_overflow_compacts_then_retries() -> None:
    client = _ScriptedClient(
        [_call("a", text="x" * 30_000), _done()],
        [_call("b", text="y" * 30_000), _done()],
        [_error("context_overflow", "maximum context length exceeded")],
        [_text("recovered"), _done()],
    )
    session = _session(client, _EchoHandler())

    result = await session.run("the task")

    assert result.status == "completed"
    assert result.text == "recovered"
    assert len(client.summary_prompts) == 1
    # The retried request is smaller than the one that overflowed.
    assert len(client.prompts[3].messages) < len(client.prompts[2].messages)
    _assert_tool_calls_paired(session.state.messages)


def test_classify_error() -> None:
    class _Status(Exception):
        def __init__(self, msg: str, status_code: int) -> None:
            super().__init__(msg)
            self.status_code = status_code

    assert classify_error(_Status("This model's maximum context length is 1", 400)) == (
        "context_overflow"
    )
    assert classify_error(_Status("bad key", 401)) == "fatal"
    assert classify_error(_Status("overloaded", 529)) == "transient"
    assert classify_error(TimeoutError("timed out")) == "transient"


async def test_max_turns_is_reported_as_its_own_status() -> None:
    client = _ScriptedClient(*[[_call(f"c{i}", text="x"), _done()] for i in range(5)])
    session = _session(client, _EchoHandler())

    result = await session.run("go", max_turns=2)

    assert result.status == "max_turns"
    assert session.state.status == "max_turns"
    assert len(client.prompts) == 2
    _assert_tool_calls_paired(session.state.messages)


async def test_cost_cap_ends_run_between_steps() -> None:
    client = _ScriptedClient(*[[_call(f"c{i}", text="x"), _done(cost=0.6)] for i in range(5)])
    session = _session(client, _EchoHandler())
    session.max_cost_usd = 1.0

    result = await session.run("go")

    assert result.status == "budget_exceeded"
    assert len(client.prompts) == 2  # $0.60, then $1.20 >= cap
    assert any(e.type == "budget_exceeded" and "Cost cap" in e.content for e in result.events)
    _assert_tool_calls_paired(session.state.messages)

    # A later run on the same session stops without calling the model again.
    again = await session.run("continue")
    assert again.status == "budget_exceeded"
    assert len(client.prompts) == 2


async def test_wall_cap_interrupts_a_long_tool_call() -> None:
    client = _ScriptedClient([_call("a", name="slow"), _done()])
    session = _session(client, _SlowHandler())
    session.max_wall_seconds = 0.2

    result = await asyncio.wait_for(session.run("go"), timeout=5)

    assert result.status == "budget_exceeded"
    assert "time cap" in session.state.messages[-1]["content"]
    _assert_tool_calls_paired(session.state.messages)


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


async def test_compaction_keeps_task_summary_and_recent_tail() -> None:
    calls = [[_call(f"c{i}", text=f"result-{i} " + "z" * 400), _done()] for i in range(6)]
    client = _ScriptedClient(*calls, [_text("final"), _done()])
    session = _session(client, _EchoHandler())
    await session.run("the original task")
    session.compact_at_tokens = 1_000  # tail budget = 250 tokens

    result = await session.compact()

    messages = session.state.messages
    assert result.tokens_after < result.tokens_before
    assert messages[0] == {"role": "user", "content": "the original task"}
    assert messages[1]["role"] == "user" and "SUMMARY" in messages[1]["content"]
    assert messages[-1] == {"role": "assistant", "content": "final"}
    assert messages[2]["role"] != "tool"
    _assert_tool_calls_paired(messages)
    # Only the head was sent for summarisation, ending with the instruction.
    sent = client.summary_prompts[0].messages
    assert sent[-1].role == "user" and "COMPACTION" in sent[-1].content
    assert len(sent) < len(calls) * 2 + 3


async def test_failed_compaction_leaves_history_unchanged() -> None:
    client = _ScriptedClient(
        [_call("a", text="x" * 4000), _done()],
        [_call("b", text="y" * 4000), _done()],
        [_text("final"), _done()],
        summary=ModelCallError("API error 500", "transient"),
    )
    session = _session(client, _EchoHandler())
    await session.run("task")
    before = list(session.state.messages)
    session.compact_at_tokens = 100

    result = await session.compact()

    assert result.error == "API error 500"
    assert session.state.messages == before
    # Not retried on the very next check; only once the context has grown.
    assert session._should_auto_compact() is False


async def test_auto_compaction_fires_from_model_table_without_explicit_window() -> None:
    big = 100_000  # above 70% of the 128K default window
    client = _ScriptedClient(
        [_call("a", text="x" * 4000), _done(input_tokens=1_000)],
        [_call("b", text="y" * 100_000), _done(input_tokens=big)],
        [_text("final"), _done(input_tokens=2_000)],
    )
    session = _session(client, _EchoHandler())
    assert session.context_window is None

    result = await session.run("task")

    assert result.status == "completed"
    assert [e.type for e in result.events].count("compact_end") == 1


def test_compact_threshold_stays_below_price_tier() -> None:
    assert compact_threshold("openai/gpt-6.1-sol") < 272_000
    assert compact_threshold("openai/gpt-5.2") == 280_000
    assert compact_threshold("unknown-model") == int(128_000 * 0.7)
    assert compact_threshold("openai/gpt-6.1-sol", compact_at_tokens=50_000) == 50_000


# ---------------------------------------------------------------------------
# Config and trajectory
# ---------------------------------------------------------------------------


def test_config_round_trips_caps(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    AgentConfig(model="m", max_cost_usd=4.0, max_wall_seconds=600, context_window=1000).to_file(
        path
    )
    loaded = AgentConfig.from_file(path)
    assert loaded.max_cost_usd == 4.0
    assert loaded.max_wall_seconds == 600
    assert loaded.context_window == 1000
    assert loaded.extras == {}


def test_trajectory_snapshot_does_not_change_builder(tmp_path: Path) -> None:
    from rho_agent.core.events import AgentEvent

    builder = TrajectoryBuilder(model="m")
    events = [
        AgentEvent(type="tool_start", tool_name="bash", tool_call_id="a", tool_args={"c": "ls"}),
        AgentEvent(type="tool_end", tool_name="bash", tool_call_id="a", tool_result="out"),
    ]
    path = tmp_path / "trajectory.json"

    builder.save_snapshot(path, events, user_input="task")
    builder.save_snapshot(path, events, user_input="task")

    saved = json.loads(path.read_text())
    assert [s["source"] for s in saved["steps"]] == ["user", "agent"]
    assert saved["steps"][1]["observations"][0]["content"] == "out"
    assert builder.to_trajectory()["steps"] == []
    assert not path.with_name("trajectory.json.tmp").exists()


# ---------------------------------------------------------------------------
# Completion gate and per-call limit
# ---------------------------------------------------------------------------


async def test_completion_gate_asks_for_verification_before_ending() -> None:
    from rho_agent.core.events import CompletionGate

    client = _ScriptedClient(
        [_text("I think I'm done."), _done()],
        [_call("a", text="ran the tests"), _done()],
        [_text("Fixed one thing."), _done()],
        [_text("OK"), _done()],
    )
    session = _session(client, _EchoHandler())
    session.completion_gate = CompletionGate(prompt="Verify. Reply {token}.", token="OK")

    result = await session.run("task")

    assert result.status == "completed"
    assert len(client.prompts) == 4
    users = [m["content"] for m in session.state.messages if m["role"] == "user"]
    assert users == ["task", "Verify. Reply OK.", "Verify. Reply OK."]
    assert [e.type for e in result.events].count("completion_gate") == 2


async def test_completion_gate_stops_after_max_rounds() -> None:
    from rho_agent.core.events import CompletionGate

    client = _ScriptedClient(*[[_text("still not saying it"), _done()] for _ in range(10)])
    session = _session(client)
    session.completion_gate = CompletionGate(token="OK", max_rounds=2)

    result = await session.run("task")

    assert result.status == "completed"
    assert len(client.prompts) == 3  # first answer + two gate rounds


async def test_gate_is_off_by_default_and_on_from_config() -> None:
    assert _session(_ScriptedClient()).completion_gate is None
    agent = MagicMock(spec=Agent)
    agent.config = AgentConfig(system_prompt="s", model="m", completion_gate=True)
    agent.system_prompt = "s"
    agent.registry = ToolRegistry()
    assert Session(agent, client=_ScriptedClient()).completion_gate is not None


class _StallingClient(_ScriptedClient):
    """First call never produces an event; later calls behave normally."""

    def __init__(self) -> None:
        super().__init__()
        self.n = 0

    async def stream(self, prompt: Any):
        self.n += 1
        if self.n == 1:
            await asyncio.sleep(30)
        yield _text("answer")
        yield _done()


async def test_stalled_model_call_is_abandoned_and_retried() -> None:
    client = _StallingClient()
    session = _session(client)
    session.max_call_seconds = 0.2

    result = await asyncio.wait_for(session.run("go"), timeout=5)

    assert result.status == "completed"
    assert result.text == "answer"
    assert client.n == 2
    assert any(e.type == "retry" and "exceeded" in e.content for e in result.events)


async def test_time_cap_interrupts_a_stalled_model_call() -> None:
    client = _StallingClient()
    session = _session(client)
    session.max_wall_seconds = 0.2

    result = await asyncio.wait_for(session.run("go"), timeout=5)

    assert result.status == "budget_exceeded"
    assert client.n == 1


# ---------------------------------------------------------------------------
# Compaction settings
# ---------------------------------------------------------------------------


def test_explicit_fraction_is_used_as_given_and_tokens_win() -> None:
    # The default is capped below the price tier; an explicit fraction is not.
    assert compact_threshold("openai/gpt-6.1-sol", threshold=0.9) == 360_000
    assert compact_threshold("unknown-model", threshold=0.5) == 64_000
    assert compact_threshold("unknown-model", context_window=1_000, threshold=0.1) == 100
    assert compact_threshold("unknown-model", threshold=0.5, compact_at_tokens=7) == 7


@pytest.mark.parametrize("bad", [0, -0.1, 1.5, 70])
def test_fraction_outside_zero_to_one_is_rejected(bad: float) -> None:
    with pytest.raises(ValueError, match="compact_threshold"):
        AgentConfig(model="m", compact_threshold=bad)
    with pytest.raises(ValueError, match="compact_at_tokens"):
        AgentConfig(model="m", compact_at_tokens=0)


def test_compaction_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "RHO_AGENT_AUTO_COMPACT",
        "RHO_AGENT_COMPACT_THRESHOLD",
        "RHO_AGENT_COMPACT_AT_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)
    defaults = AgentConfig(model="m")
    assert (defaults.auto_compact, defaults.compact_threshold, defaults.compact_at_tokens) == (
        True,
        None,
        None,
    )

    monkeypatch.setenv("RHO_AGENT_AUTO_COMPACT", "0")
    monkeypatch.setenv("RHO_AGENT_COMPACT_THRESHOLD", "0.25")
    monkeypatch.setenv("RHO_AGENT_COMPACT_AT_TOKENS", "40000")
    from_env = AgentConfig(model="m")
    assert (from_env.auto_compact, from_env.compact_threshold, from_env.compact_at_tokens) == (
        False,
        0.25,
        40_000,
    )
    # An explicit argument beats the environment.
    assert AgentConfig(model="m", compact_threshold=0.5).compact_threshold == 0.5

    monkeypatch.setenv("RHO_AGENT_COMPACT_THRESHOLD", "lots")
    with pytest.raises(ValueError, match="RHO_AGENT_COMPACT_THRESHOLD"):
        AgentConfig(model="m")


def test_compaction_settings_round_trip_through_yaml(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    AgentConfig(
        model="m", auto_compact=False, compact_threshold=0.4, compact_at_tokens=9000
    ).to_file(path)
    loaded = AgentConfig.from_file(path)
    assert (loaded.auto_compact, loaded.compact_threshold, loaded.compact_at_tokens) == (
        False,
        0.4,
        9000,
    )
    assert not loaded.extras


async def test_session_compacts_at_the_configured_fraction() -> None:
    def run_with(**settings: Any):
        client = _ScriptedClient(
            [_call("a", text="x" * 4000), _done(input_tokens=1_000)],
            [_call("b", text="y" * 4000), _done(input_tokens=30_000)],
            [_text("final"), _done(input_tokens=2_000)],
        )
        session = _session(client, _EchoHandler())
        for key, value in settings.items():
            setattr(session, key, value)
        return session

    # 30K input is under the default threshold (70% of 128K): no compaction.
    result = await run_with().run("task")
    assert [e.type for e in result.events].count("compact_end") == 0

    # At 20% of the window (25.6K) the same run compacts once.
    result = await run_with(compact_threshold=0.2).run("task")
    assert [e.type for e in result.events].count("compact_end") == 1

    # Turned off, it never compacts whatever the threshold.
    result = await run_with(compact_threshold=0.2, auto_compact=False).run("task")
    assert [e.type for e in result.events].count("compact_end") == 0


def test_session_takes_compaction_settings_from_the_agent_config() -> None:
    agent = Agent(AgentConfig(model="m", auto_compact=False, compact_threshold=0.3))
    session = Session(agent, client=_ScriptedClient())
    assert session.auto_compact is False
    assert session.compact_threshold == 0.3
    assert session._compact_threshold() == int(128_000 * 0.3)


def test_cli_compaction_flags_only_override_what_was_passed() -> None:
    import typer

    from rho_agent.cli import app
    from rho_agent.cli.main_cmd import _apply_compaction_flags

    config = AgentConfig(model="m", compact_threshold=0.6)
    _apply_compaction_flags(config, None, 5000, False)
    assert (config.compact_threshold, config.compact_at_tokens, config.auto_compact) == (
        0.6,
        5000,
        False,
    )

    # Read the declared options, not the help text, which is styled in CI.
    command = typer.main.get_command(app).commands["main"]
    declared = {opt for param in command.params for opt in (*param.opts, *param.secondary_opts)}
    assert {"--compact-threshold", "--compact-at-tokens", "--no-auto-compact"} <= declared


# ---------------------------------------------------------------------------
# Compaction quality
# ---------------------------------------------------------------------------


async def _session_with_history(
    client: _ScriptedClient, *prompts: str, output_chars: int = 400
) -> Session:
    """Run each prompt through three tool calls so there is history to compact."""
    session = _session(client, _EchoHandler())
    for n, prompt in enumerate(prompts):
        client._calls.extend(
            [[_call(f"p{n}c{i}", text=f"out-{n}-{i} " + "z" * output_chars), _done()]][0]
            for i in range(3)
        )
        client._calls.append([_text(f"answer {n}"), _done()])
        await session.run(prompt)
    session.compact_at_tokens = 1_000  # tail budget = 250 tokens
    return session


@pytest.mark.parametrize("empty", ["", "   \n"])
async def test_empty_summary_is_a_failure_and_history_is_kept(empty: str) -> None:
    client = _ScriptedClient(summary=empty)
    session = await _session_with_history(client, "task")
    before = list(session.state.messages)

    result = await session.compact()

    assert result.error and "empty summary" in result.error
    assert session.state.messages == before


async def test_summary_request_keeps_tools_but_forbids_calling_them() -> None:
    client = _ScriptedClient()
    session = await _session_with_history(client, "task")

    await session.compact()

    prompt = client.summary_prompts[0]
    assert prompt.tools  # still sent, so the cached prefix is reused
    assert prompt.tool_choice == "none"
    assert "## Tried and ruled out" in prompt.messages[-1].content


def test_litellm_client_sends_tool_choice() -> None:
    pytest.importorskip("litellm")  # optional dependency (the evals extra)
    from rho_agent.client.litellm_client import LiteLLMClient
    from rho_agent.client.model import Message, Prompt

    client = LiteLLMClient(model="openai/test")
    tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    messages = [Message(role="user", content="hi")]

    plain = client._build_kwargs(prompt=Prompt(system="s", messages=messages, tools=tools))
    forced = client._build_kwargs(
        prompt=Prompt(system="s", messages=messages, tools=tools, tool_choice="none")
    )

    assert "tool_choice" not in plain
    assert forced["tool_choice"] == "none" and forced["tools"] == tools


async def test_every_user_message_survives_compaction_verbatim() -> None:
    client = _ScriptedClient()
    session = await _session_with_history(
        client, "the original task", "also keep the API stable", "and use tabs"
    )

    await session.compact()
    # A second round must not stack summaries or lose the user's words.
    client._calls.extend([[_call(f"x{i}", text="w" * 400), _done()] for i in range(3)])
    client._calls.append([_text("more"), _done()])
    await session.run("one more thing")
    await session.compact()

    messages = session.state.messages
    users = [m["content"] for m in messages if m["role"] == "user"]
    summaries = [u for u in users if "SUMMARY" in u]
    assert len(summaries) == 1
    spoken = [u for u in users if "SUMMARY" not in u]
    assert spoken[:3] == ["the original task", "also keep the API stable", "and use tabs"]
    assert "one more thing" in spoken
    _assert_tool_calls_paired(messages)


async def test_an_oversized_later_user_message_is_summarised_not_kept() -> None:
    client = _ScriptedClient()
    huge = "paste " * 20_000  # far above the verbatim budget
    session = await _session_with_history(client, "task", huge, "short follow-up")

    await session.compact()

    users = [m["content"] for m in session.state.messages if m["role"] == "user"]
    assert "task" in users and "short follow-up" in users
    assert huge not in users


async def test_auto_compaction_trims_old_tool_output_without_a_model_call() -> None:
    client = _ScriptedClient()
    session = await _session_with_history(client, "task", output_chars=20_000)
    before = list(session.state.messages)

    result = await session.compact(trigger="auto")

    assert result.method == "prune" and result.error is None
    assert client.summary_prompts == []  # no summarisation call was made
    # The echo tool's arguments hold a second copy of the text, which stays.
    assert result.tokens_after < result.tokens_before * 0.6
    messages = session.state.messages
    assert len(messages) == len(before)  # nothing dropped, only shortened
    assert [m["role"] for m in messages] == [m["role"] for m in before]
    trimmed = [m for m in messages if m["role"] == "tool" and "were removed" in m["content"]]
    # The start and end of each result are kept, so it is still recognisable.
    assert trimmed and all("out-0-" in m["content"] for m in trimmed)
    assert all(len(m["content"]) < 1_000 for m in trimmed)
    _assert_tool_calls_paired(messages)

    # A second automatic round finds nothing left to trim, so it summarises.
    result = await session.compact(trigger="auto")
    assert result.method == "summary" and len(client.summary_prompts) == 1


async def test_manual_compaction_always_summarises() -> None:
    client = _ScriptedClient()
    session = await _session_with_history(client, "task", output_chars=20_000)

    result = await session.compact()

    assert result.method == "summary" and len(client.summary_prompts) == 1


async def test_small_tool_output_goes_straight_to_a_summary() -> None:
    client = _ScriptedClient()
    session = await _session_with_history(client, "task", output_chars=400)

    result = await session.compact(trigger="auto")

    assert result.method == "summary" and len(client.summary_prompts) == 1
