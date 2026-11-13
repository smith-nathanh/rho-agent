from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from rho_agent.cli import run_single, run_single_with_output
from rho_agent.core.events import AgentEvent, RunResult


def _make_error_session():
    """Create a mock Session whose run() emits an error event."""
    mock_session = AsyncMock()
    mock_session.cancel = MagicMock()

    async def fake_run(prompt, *, on_event=None):
        if on_event:
            event = AgentEvent(type="error", content="boom")
            result = on_event(event)
            if result is not None:
                await result
        return RunResult(text="", events=[], status="error", usage={})

    mock_session.run = fake_run
    return mock_session


@pytest.mark.asyncio
async def test_run_single_handles_error_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rho_agent.cli.single.handle_event", lambda event, **kwargs: None)
    monkeypatch.setattr("rho_agent.cli.single.platform.system", lambda: "Windows")

    session = _make_error_session()
    # Should not raise
    await run_single(session, "prompt")


@pytest.mark.asyncio
async def test_run_single_with_output_returns_false_on_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("rho_agent.cli.single.handle_event", lambda event, **kwargs: None)
    monkeypatch.setattr("rho_agent.cli.single.platform.system", lambda: "Windows")

    session = _make_error_session()
    output_path = tmp_path / "response.txt"

    result = await run_single_with_output(session, "prompt", str(output_path))

    assert result is False
    assert not output_path.exists()


def test_status_bar_shows_reasoning_tokens_only_when_there_are_some() -> None:
    from rho_agent.cli.formatting import TokenStatus

    status = TokenStatus()
    status.update({"context_size": 900, "total_input_tokens": 1200, "total_output_tokens": 300})
    assert status.render() == "context:900, session: 1200 in | 300 out"

    status.update({"total_reasoning_tokens": 120})
    assert status.render() == "context:900, session: 1200 in | 300 out (120 reasoning)"
