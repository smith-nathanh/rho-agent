"""Tests for the Session agentic loop."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rho_agent.client.model import StreamEvent, ToolCall
from rho_agent.core.agent import Agent
from rho_agent.core.config import AgentConfig
from rho_agent.core.events import AgentEvent, ApprovalInterrupt
from rho_agent.core.session import Session
from rho_agent.core.state import State
from rho_agent.tools.base import ToolHandler, ToolInvocation, ToolOutput
from rho_agent.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _EchoHandler(ToolHandler):
    """Handler that echoes arguments back."""

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
        return ToolOutput(content=invocation.arguments.get("text", ""))


class _ApprovalEchoHandler(_EchoHandler):
    """Echo handler that requires approval."""

    @property
    def requires_approval(self) -> bool:
        return True


def _make_agent(registry: ToolRegistry | None = None) -> Agent:
    """Build an Agent with mocked internals."""
    agent = MagicMock(spec=Agent)
    agent.config = AgentConfig(
        system_prompt="You are a test agent.",
        profile="readonly",
        model="test-model",
    )
    agent.system_prompt = "You are a test agent."
    agent.registry = registry or ToolRegistry()
    agent.create_client = MagicMock()
    return agent


def _make_session(
    agent: Agent | None = None,
    client: Any = None,
    registry: ToolRegistry | None = None,
) -> Session:
    """Build a Session wired to the given mocks."""
    agent = agent or _make_agent(registry=registry)
    session = Session(agent, client=client or MagicMock())
    if registry is not None:
        session._registry = registry
    return session


async def _stream_events(*events: StreamEvent):
    """Async generator that yields StreamEvents."""
    for e in events:
        yield e


def _make_stream(*events: StreamEvent):
    """Return a callable that produces a fresh async generator of events per call."""

    def factory(prompt):
        async def gen():
            for e in events:
                yield e

        return gen()

    return factory


def _make_multi_turn_stream(*turn_events: tuple[StreamEvent, ...]):
    """Return a callable that yields different events per call."""
    calls = iter(turn_events)

    def factory(prompt):
        events = next(calls)
        return _stream_events(*events)

    return factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_response_ends_loop():
    """Text + done with no tool calls → run() returns text, status completed."""
    client = MagicMock()
    client.stream = _make_stream(
        StreamEvent(type="text", content="Hello!"),
        StreamEvent(type="done", usage={"input_tokens": 10, "output_tokens": 5}),
    )

    session = _make_session(client=client)
    result = await session.run("Hi")

    assert result.text == "Hello!"
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_tool_call_then_text_response():
    """Tool call turn 1, text turn 2 → dispatch called, final text collected."""
    registry = ToolRegistry()
    registry.register(_EchoHandler())

    client = MagicMock()
    client.stream = _make_multi_turn_stream(
        (
            StreamEvent(
                type="tool_call",
                tool_call=ToolCall(id="tc_1", name="echo", arguments={"text": "echoed"}),
            ),
            StreamEvent(type="done", usage={"input_tokens": 10, "output_tokens": 5}),
        ),
        (
            StreamEvent(type="text", content="Done!"),
            StreamEvent(type="done", usage={"input_tokens": 15, "output_tokens": 3}),
        ),
    )

    session = _make_session(client=client, registry=registry)
    result = await session.run("Use echo")

    assert result.text == "Done!"
    assert result.status == "completed"
    tool_end_events = [e for e in result.events if e.type == "tool_end"]
    assert len(tool_end_events) == 1
    assert tool_end_events[0].tool_result == "echoed"


@pytest.mark.asyncio
async def test_max_turns_respected():
    """Model always returns tool calls, max_turns=2 → loop stops after 2."""
    registry = ToolRegistry()
    registry.register(_EchoHandler())

    client = MagicMock()
    client.stream = _make_stream(
        StreamEvent(
            type="tool_call",
            tool_call=ToolCall(id="tc_x", name="echo", arguments={"text": "again"}),
        ),
        StreamEvent(type="done", usage={"input_tokens": 10, "output_tokens": 5}),
    )

    session = _make_session(client=client, registry=registry)
    result = await session.run("loop forever", max_turns=2)

    tool_starts = [e for e in result.events if e.type == "tool_start"]
    assert len(tool_starts) == 2


@pytest.mark.asyncio
async def test_cancel_before_model_call():
    """cancel_check triggers before first model call → status cancelled."""
    client = MagicMock()

    session = _make_session(client=client)
    # cancel_check fires on the first _is_cancelled() call inside the loop
    session.cancel_check = lambda: True

    result = await session.run("Should not run")

    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_via_cancel_check():
    """cancel_check returns True → cancellation mid-loop."""
    client = MagicMock()

    session = _make_session(client=client)
    session.cancel_check = lambda: True

    result = await session.run("Should cancel")

    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_approval_rejection_ends_turn():
    """approval_callback returns False → tool_blocked event, turn ends."""
    registry = ToolRegistry()
    registry.register(_ApprovalEchoHandler())

    client = MagicMock()
    client.stream = _make_stream(
        StreamEvent(
            type="tool_call",
            tool_call=ToolCall(id="tc_1", name="echo", arguments={"text": "blocked"}),
        ),
        StreamEvent(type="done", usage={"input_tokens": 10, "output_tokens": 5}),
    )

    session = _make_session(client=client, registry=registry)
    session.approval_callback = AsyncMock(return_value=False)

    result = await session.run("Try echo")

    blocked_events = [e for e in result.events if e.type == "tool_blocked"]
    assert len(blocked_events) == 1
    assert blocked_events[0].tool_name == "echo"


@pytest.mark.asyncio
async def test_approval_interrupt_yields_interruption():
    """Callback raises ApprovalInterrupt → interruption event."""
    registry = ToolRegistry()
    registry.register(_ApprovalEchoHandler())

    client = MagicMock()
    client.stream = _make_stream(
        StreamEvent(
            type="tool_call",
            tool_call=ToolCall(id="tc_1", name="echo", arguments={"text": "interrupt"}),
        ),
        StreamEvent(type="done", usage={"input_tokens": 10, "output_tokens": 5}),
    )

    session = _make_session(client=client, registry=registry)
    session.approval_callback = AsyncMock(side_effect=ApprovalInterrupt("paused"))

    result = await session.run("Try echo")

    interruption_events = [e for e in result.events if e.type == "interruption"]
    assert len(interruption_events) == 1


@pytest.mark.asyncio
async def test_auto_compact_triggers():
    """Context above threshold → compact() called."""
    client = MagicMock()
    client.stream = _make_stream(
        StreamEvent(type="text", content="response"),
        StreamEvent(type="done", usage={"input_tokens": 10, "output_tokens": 5}),
    )

    session = _make_session(client=client)
    session.auto_compact = True
    session.context_window = 100
    session._state.add_user_message("x" * 500)
    session._last_input_tokens = 80  # above 70% of 100

    with patch.object(session, "compact", new_callable=AsyncMock) as mock_compact:
        mock_compact.return_value = MagicMock(
            summary="compacted", tokens_before=80, tokens_after=20, trigger="auto"
        )
        await session.run("more input")

    mock_compact.assert_called()


@pytest.mark.asyncio
async def test_error_from_llm_yields_error_event():
    """StreamEvent(type='error') → run returns status 'error'."""
    client = MagicMock()
    client.stream = _make_stream(
        StreamEvent(type="error", content="API error 500"),
    )

    session = _make_session(client=client)
    result = await session.run("Fail please")

    assert result.status == "error"
    error_events = [e for e in result.events if e.type == "error"]
    assert len(error_events) == 1


@pytest.mark.asyncio
async def test_usage_accumulated_across_turns():
    """Two turns → turn_complete usage reflects accumulated totals."""
    registry = ToolRegistry()
    registry.register(_EchoHandler())

    client = MagicMock()
    client.stream = _make_multi_turn_stream(
        (
            StreamEvent(
                type="tool_call",
                tool_call=ToolCall(id="tc_1", name="echo", arguments={"text": "a"}),
            ),
            StreamEvent(type="done", usage={"input_tokens": 100, "output_tokens": 50}),
        ),
        (
            StreamEvent(type="text", content="final"),
            StreamEvent(type="done", usage={"input_tokens": 200, "output_tokens": 80}),
        ),
    )

    session = _make_session(client=client, registry=registry)
    result = await session.run("Go")

    turn_events = [e for e in result.events if e.type == "turn_complete"]
    assert len(turn_events) == 1
    usage = turn_events[0].usage
    assert usage["total_input_tokens"] == 300
    assert usage["total_output_tokens"] == 130
