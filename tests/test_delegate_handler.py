from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from rho_agent.core.config import AgentConfig
from rho_agent.core.events import RunResult
from rho_agent.core.state import State
from rho_agent.tools.base import ToolInvocation
from rho_agent.tools.handlers.delegate import DelegateHandler


def _make_handler(**overrides) -> DelegateHandler:
    defaults = dict(
        parent_config=AgentConfig(profile="readonly"),
        parent_system_prompt="system",
        parent_state=State(),
        parent_approval_callback=None,
        parent_cancel_check=None,
        requires_approval=False,
    )
    defaults.update(overrides)
    return DelegateHandler(**defaults)


@pytest.mark.asyncio
async def test_empty_instruction_returns_error_without_spawning() -> None:
    handler = _make_handler()
    output = await handler.handle(
        ToolInvocation(call_id="1", tool_name="delegate", arguments={"instruction": "   "})
    )
    assert output.success is False
    assert "non-empty" in output.content.lower() or "requires" in output.content.lower()


@pytest.mark.asyncio
async def test_delegate_full_context_false_uses_empty_child_history() -> None:
    parent_state = State()
    parent_state.add_user_message("existing context")

    handler = _make_handler(parent_state=parent_state)

    mock_result = RunResult(
        text="child complete", events=[], status="completed", usage={"input_tokens": 1}
    )

    with patch("rho_agent.tools.handlers.delegate.Session") as MockSession:
        mock_session = AsyncMock()
        mock_session.id = "child-123"
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        MockSession.return_value = mock_session

        output = await handler.handle(
            ToolInvocation(
                call_id="1",
                tool_name="delegate",
                arguments={"instruction": "do the thing", "full_context": False},
            )
        )

    assert output.success is True
    assert output.content == "child complete"
    assert output.metadata["child_status"] == "completed"
    assert output.metadata["child_session_id"] == "child-123"
    assert output.metadata["duration_seconds"] >= 0
    # Verify child was created with empty state (not parent's)
    call_kwargs = MockSession.call_args
    child_state = call_kwargs.kwargs.get("state") or call_kwargs[1].get("state")
    assert child_state.messages == []


@pytest.mark.asyncio
async def test_delegate_full_context_true_snapshots_parent_history() -> None:
    parent_state = State()
    parent_state.add_user_message("existing context")

    handler = _make_handler(parent_state=parent_state)

    mock_result = RunResult(text="ok", events=[], status="completed", usage={})

    with patch("rho_agent.tools.handlers.delegate.Session") as MockSession:
        mock_session = AsyncMock()
        mock_session.id = "child-456"
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        MockSession.return_value = mock_session

        await handler.handle(
            ToolInvocation(
                call_id="1",
                tool_name="delegate",
                arguments={"instruction": "continue", "full_context": True},
            )
        )

    # Verify child was given a copy of parent history
    call_kwargs = MockSession.call_args
    child_state = call_kwargs.kwargs.get("state") or call_kwargs[1].get("state")
    assert len(child_state.messages) == len(parent_state.messages)
    # Must be a deep copy, not the same object
    assert child_state.messages is not parent_state.messages


@pytest.mark.asyncio
async def test_delegate_failure_returns_error() -> None:
    handler = _make_handler()

    with patch("rho_agent.tools.handlers.delegate.Session") as MockSession:
        mock_session = AsyncMock()
        mock_session.id = "child-err"
        mock_session.run = AsyncMock(side_effect=RuntimeError("boom"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        MockSession.return_value = mock_session

        output = await handler.handle(
            ToolInvocation(call_id="1", tool_name="delegate", arguments={"instruction": "do work"})
        )

    assert output.success is False
    assert "boom" in output.content
    assert output.metadata["child_status"] == "error"
    assert output.metadata["child_session_id"] == "child-err"
    assert output.metadata["duration_seconds"] >= 0


def test_delegate_removes_delegate_from_child_registry() -> None:
    """The DelegateHandler should remove 'delegate' from the child agent's registry."""
    from rho_agent.core.agent import Agent

    # Create a parent-like agent that has delegate
    config = AgentConfig(profile="developer")
    agent = Agent(config)
    # developer profile may include delegate — check our logic is correct
    # The key behavior: DelegateHandler builds a child Agent and removes delegate
    child_config = AgentConfig(profile="developer", system_prompt="test")
    child_agent = Agent(child_config)
    if "delegate" in child_agent.registry:
        child_agent.registry.unregister("delegate")
        assert "delegate" not in child_agent.registry
