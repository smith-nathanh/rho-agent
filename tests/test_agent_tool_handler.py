from __future__ import annotations

import pytest

from rho_agent.capabilities import CapabilityProfile
from rho_agent.core.session import Session
from rho_agent.runtime.options import RuntimeOptions
from rho_agent.runtime.types import RunResult
from rho_agent.tools.base import ToolInvocation
from rho_agent.tools.handlers.agent_tool import AgentToolHandler, _default_formatter


class FakeRuntime:
    def __init__(self, *, session_id: str = "child-001") -> None:
        self.session_id = session_id
        self.close_status = "completed"

    async def start(self) -> None:
        pass

    async def close(self, status: str = "completed") -> None:
        pass

    async def __aenter__(self) -> FakeRuntime:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close(self.close_status)


def _make_handler(**overrides: object) -> AgentToolHandler:
    defaults = dict(
        tool_name="test_agent",
        tool_description="A test agent tool.",
        system_prompt="You are a test agent.",
        options=RuntimeOptions(profile=CapabilityProfile.readonly()),
    )
    defaults.update(overrides)
    return AgentToolHandler(**defaults)


@pytest.mark.asyncio
async def test_empty_instruction_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _make_handler()
    output = await handler.handle(
        ToolInvocation(call_id="1", tool_name="test_agent", arguments={"instruction": "   "})
    )
    assert output.success is False
    assert "empty" in output.content.lower()


@pytest.mark.asyncio
async def test_successful_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _make_handler()
    captured: dict[str, object] = {}

    def fake_create_runtime(
        system_prompt: str, *, options: object = None, session: object = None,
        approval_callback: object = None, cancel_check: object = None,
    ) -> object:
        captured["system_prompt"] = system_prompt
        captured["options"] = options
        captured["session"] = session
        return FakeRuntime()

    async def fake_run_prompt(runtime: object, prompt: str) -> RunResult:
        captured["prompt"] = prompt
        return RunResult(text="result text", events=[], status="completed", usage={"input_tokens": 10})

    monkeypatch.setattr("rho_agent.runtime.factory.create_runtime", fake_create_runtime)
    monkeypatch.setattr("rho_agent.runtime.run.run_prompt", fake_run_prompt)

    output = await handler.handle(
        ToolInvocation(call_id="1", tool_name="test_agent", arguments={"instruction": "do work"})
    )

    assert output.success is True
    assert output.content == "result text"
    assert output.metadata["child_status"] == "completed"
    assert output.metadata["child_usage"] == {"input_tokens": 10}
    assert output.metadata["duration_seconds"] >= 0
    # Child gets its own system prompt, not parent's
    assert captured["system_prompt"] == "You are a test agent."
    # Child session is independent
    child_session = captured["session"]
    assert isinstance(child_session, Session)
    assert child_session.history == []
    # Delegation disabled in child
    child_options = captured["options"]
    assert isinstance(child_options, RuntimeOptions)
    assert child_options.enable_delegate is False


@pytest.mark.asyncio
async def test_child_failure_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _make_handler()

    def fake_create_runtime(*a: object, **kw: object) -> object:
        return FakeRuntime()

    async def fake_run_prompt(runtime: object, prompt: str) -> RunResult:
        raise RuntimeError("boom")

    monkeypatch.setattr("rho_agent.runtime.factory.create_runtime", fake_create_runtime)
    monkeypatch.setattr("rho_agent.runtime.run.run_prompt", fake_run_prompt)

    output = await handler.handle(
        ToolInvocation(call_id="1", tool_name="test_agent", arguments={"instruction": "do work"})
    )

    assert output.success is False
    assert "boom" in output.content
    assert output.metadata["child_status"] == "error"


@pytest.mark.asyncio
async def test_typed_parameters_formatted(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _make_handler(
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "dialect": {"type": "string"},
            },
            "required": ["question"],
        },
    )
    captured_prompt: str | None = None

    def fake_create_runtime(*a: object, **kw: object) -> object:
        return FakeRuntime()

    async def fake_run_prompt(runtime: object, prompt: str) -> RunResult:
        nonlocal captured_prompt
        captured_prompt = prompt
        return RunResult(text="SELECT 1", events=[], status="completed", usage={})

    monkeypatch.setattr("rho_agent.runtime.factory.create_runtime", fake_create_runtime)
    monkeypatch.setattr("rho_agent.runtime.run.run_prompt", fake_run_prompt)

    await handler.handle(
        ToolInvocation(
            call_id="1", tool_name="test_agent",
            arguments={"question": "How many users?", "dialect": "sqlite"},
        )
    )

    assert captured_prompt is not None
    assert "question: How many users?" in captured_prompt
    assert "dialect: sqlite" in captured_prompt


@pytest.mark.asyncio
async def test_custom_input_formatter(monkeypatch: pytest.MonkeyPatch) -> None:
    def custom_fmt(args: dict) -> str:
        return f"SQL for: {args['question']}"

    handler = _make_handler(input_formatter=custom_fmt)
    captured_prompt: str | None = None

    def fake_create_runtime(*a: object, **kw: object) -> object:
        return FakeRuntime()

    async def fake_run_prompt(runtime: object, prompt: str) -> RunResult:
        nonlocal captured_prompt
        captured_prompt = prompt
        return RunResult(text="ok", events=[], status="completed", usage={})

    monkeypatch.setattr("rho_agent.runtime.factory.create_runtime", fake_create_runtime)
    monkeypatch.setattr("rho_agent.runtime.run.run_prompt", fake_run_prompt)

    await handler.handle(
        ToolInvocation(call_id="1", tool_name="test_agent", arguments={"question": "count users"})
    )

    assert captured_prompt == "SQL for: count users"


def test_tool_spec_uses_custom_name_and_schema() -> None:
    handler = _make_handler(
        tool_name="generate_sql",
        tool_description="Generate SQL queries.",
        input_schema={
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    )
    spec = handler.to_spec()
    assert spec["function"]["name"] == "generate_sql"
    assert spec["function"]["description"] == "Generate SQL queries."
    assert spec["function"]["parameters"]["properties"]["question"]["type"] == "string"


def test_default_formatter_single_instruction() -> None:
    assert _default_formatter({"instruction": "do it"}) == "do it"


def test_default_formatter_multi_key() -> None:
    result = _default_formatter({"question": "how many?", "limit": 10})
    assert "question: how many?" in result
    assert "limit: 10" in result


def test_requires_approval_default_false() -> None:
    handler = _make_handler()
    assert handler.requires_approval is False


def test_requires_approval_configurable() -> None:
    handler = _make_handler(requires_approval=True)
    assert handler.requires_approval is True
