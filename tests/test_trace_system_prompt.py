"""The system prompt and tool names are written to the trace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rho_agent.client.model import StreamEvent
from rho_agent.core.agent import Agent
from rho_agent.core.config import AgentConfig
from rho_agent.core.session import Session
from rho_agent.core.state import State


class _Client:
    async def stream(self, prompt: Any):
        yield StreamEvent(type="text", content="ok")
        yield StreamEvent(type="done", usage={"input_tokens": 1, "output_tokens": 1})


def _events(path: Path, kind: str) -> list[dict[str, Any]]:
    events = [json.loads(line) for line in path.read_text().splitlines()]
    return [e for e in events if e["type"] == kind]


async def test_system_prompt_is_recorded_once_until_it_changes(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    agent = Agent(AgentConfig(system_prompt="Be terse.", working_dir=str(tmp_path)))
    session = Session(agent, client=_Client(), state=State(trace_path=trace))

    await session.run("one")
    await session.run("two")

    [event] = _events(trace, "system_prompt")
    assert event["content"].startswith("Be terse.")
    assert event["tools"] == [spec["function"]["name"] for spec in agent.registry.get_specs()]

    agent.registry.unregister(event["tools"][0])
    await session.run("three")  # the tool list changed, so it is written again
    assert len(_events(trace, "system_prompt")) == 2


async def test_recorded_prompt_includes_project_instructions(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Always use tabs.")
    trace = tmp_path / "trace.jsonl"
    config = AgentConfig(
        system_prompt="Be terse.", working_dir=str(tmp_path), project_instructions=True
    )
    agent = Agent(config)
    session = Session(agent, client=_Client(), state=State(trace_path=trace))

    await session.run("go")

    [event] = _events(trace, "system_prompt")
    assert "Always use tabs." in event["content"]  # the final text the model received


async def test_replay_ignores_the_event(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    agent = Agent(AgentConfig(system_prompt="Be terse.", working_dir=str(tmp_path)))
    session = Session(agent, client=_Client(), state=State(trace_path=trace))
    await session.run("go")

    state = State.from_jsonl(trace.read_bytes())
    assert [m["role"] for m in state.messages] == ["user", "assistant"]
