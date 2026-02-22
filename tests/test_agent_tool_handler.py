from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from rho_agent.core.config import AgentConfig
from rho_agent.core.events import RunResult
from rho_agent.tools.base import ToolInvocation
from rho_agent.tools.handlers.agent_tool import AgentToolHandler, _default_formatter


def _make_handler(**overrides) -> AgentToolHandler:
    defaults = dict(
        tool_name="test_agent",
        tool_description="A test agent tool.",
        system_prompt="You are a test agent.",
        config=AgentConfig(profile="readonly"),
    )
    defaults.update(overrides)
    return AgentToolHandler(**defaults)


@pytest.mark.asyncio
async def test_empty_instruction_returns_error() -> None:
    handler = _make_handler()
    output = await handler.handle(
        ToolInvocation(call_id="1", tool_name="test_agent", arguments={"instruction": "   "})
    )
    assert output.success is False
    assert "empty" in output.content.lower()


@pytest.mark.asyncio
async def test_successful_invocation() -> None:
    handler = _make_handler()
    mock_result = RunResult(
        text="result text", events=[], status="completed", usage={"input_tokens": 10}
    )

    with patch("rho_agent.tools.handlers.agent_tool.Session") as MockSession:
        mock_session = AsyncMock()
        mock_session.id = "child-001"
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        MockSession.return_value = mock_session

        output = await handler.handle(
            ToolInvocation(
                call_id="1", tool_name="test_agent", arguments={"instruction": "do work"}
            )
        )

    assert output.success is True
    assert output.content == "result text"
    assert output.metadata["child_status"] == "completed"
    assert output.metadata["child_usage"] == {"input_tokens": 10}
    assert output.metadata["child_session_id"] == "child-001"
    assert output.metadata["duration_seconds"] >= 0
    # Verify instruction was passed to session.run
    mock_session.run.assert_called_once_with("do work")


@pytest.mark.asyncio
async def test_child_failure_returns_error() -> None:
    handler = _make_handler()

    with patch("rho_agent.tools.handlers.agent_tool.Session") as MockSession:
        mock_session = AsyncMock()
        mock_session.id = "child-err"
        mock_session.run = AsyncMock(side_effect=RuntimeError("boom"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        MockSession.return_value = mock_session

        output = await handler.handle(
            ToolInvocation(
                call_id="1", tool_name="test_agent", arguments={"instruction": "do work"}
            )
        )

    assert output.success is False
    assert "boom" in output.content
    assert output.metadata["child_status"] == "error"


@pytest.mark.asyncio
async def test_typed_parameters_formatted() -> None:
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
    mock_result = RunResult(text="SELECT 1", events=[], status="completed", usage={})

    with patch("rho_agent.tools.handlers.agent_tool.Session") as MockSession:
        mock_session = AsyncMock()
        mock_session.id = "child-001"
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        MockSession.return_value = mock_session

        await handler.handle(
            ToolInvocation(
                call_id="1",
                tool_name="test_agent",
                arguments={"question": "How many users?", "dialect": "sqlite"},
            )
        )

    captured_prompt = mock_session.run.call_args[0][0]
    assert "question: How many users?" in captured_prompt
    assert "dialect: sqlite" in captured_prompt


@pytest.mark.asyncio
async def test_custom_input_formatter() -> None:
    def custom_fmt(args: dict) -> str:
        return f"SQL for: {args['question']}"

    handler = _make_handler(input_formatter=custom_fmt)
    mock_result = RunResult(text="ok", events=[], status="completed", usage={})

    with patch("rho_agent.tools.handlers.agent_tool.Session") as MockSession:
        mock_session = AsyncMock()
        mock_session.id = "child-001"
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        MockSession.return_value = mock_session

        await handler.handle(
            ToolInvocation(
                call_id="1", tool_name="test_agent", arguments={"question": "count users"}
            )
        )

    mock_session.run.assert_called_once_with("SQL for: count users")


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
