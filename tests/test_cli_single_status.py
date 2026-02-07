from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from rho_agent.cli import run_single, run_single_with_output
from rho_agent.core.agent import AgentEvent


class ErrorAgent:
    async def run_turn(self, user_input: str) -> AsyncIterator[AgentEvent]:
        del user_input
        yield AgentEvent(type="error", content="boom")

    def request_cancel(self) -> None:
        return None


@pytest.mark.asyncio
async def test_run_single_sets_error_status_on_error_event(monkeypatch: pytest.MonkeyPatch) -> None:
    statuses: list[str] = []

    async def fake_start_runtime(runtime: object) -> None:
        return None

    async def fake_close_runtime(runtime: object, status: str) -> None:
        statuses.append(status)

    monkeypatch.setattr("rho_agent.cli.start_runtime", fake_start_runtime)
    monkeypatch.setattr("rho_agent.cli.close_runtime", fake_close_runtime)
    monkeypatch.setattr("rho_agent.cli.handle_event", lambda event: None)
    monkeypatch.setattr("rho_agent.cli.platform.system", lambda: "Windows")

    runtime = SimpleNamespace(agent=ErrorAgent(), observability=None)
    await run_single(runtime, "prompt")

    assert statuses == ["error"]


@pytest.mark.asyncio
async def test_run_single_with_output_returns_false_and_sets_error_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    statuses: list[str] = []

    async def fake_start_runtime(runtime: object) -> None:
        return None

    async def fake_close_runtime(runtime: object, status: str) -> None:
        statuses.append(status)

    monkeypatch.setattr("rho_agent.cli.start_runtime", fake_start_runtime)
    monkeypatch.setattr("rho_agent.cli.close_runtime", fake_close_runtime)
    monkeypatch.setattr("rho_agent.cli.handle_event", lambda event: None)
    monkeypatch.setattr("rho_agent.cli.platform.system", lambda: "Windows")

    runtime = SimpleNamespace(agent=ErrorAgent(), observability=None)
    output_path = tmp_path / "response.txt"

    result = await run_single_with_output(runtime, "prompt", str(output_path))

    assert result is False
    assert statuses == ["error"]
    assert not output_path.exists()
