"""End-to-end test of the Harbor runner with a scripted model (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from rho_agent.client.model import StreamEvent, ToolCall
from rho_agent.core.state import State
from rho_agent.eval.harbor import runner


class _FakeModelClient:
    """Stands in for ModelClient; replays scripted turns."""

    script: ClassVar[list[list[StreamEvent]]] = []
    priced: ClassVar[bool] = True
    instances: ClassVar[list[_FakeModelClient]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.prompts: list[Any] = []
        self._turns = list(type(self).script)
        type(self).instances.append(self)

    def can_price(self) -> bool:
        return type(self).priced

    async def stream(self, prompt: Any):
        self.prompts.append(prompt)
        for event in self._turns.pop(0):
            yield event


def _done(cost: float) -> StreamEvent:
    return StreamEvent(
        type="done",
        usage={"input_tokens": 100, "output_tokens": 10, "cached_tokens": 50, "cost_usd": cost},
    )


def _bash(call_id: str, command: str) -> StreamEvent:
    return StreamEvent(
        type="tool_call",
        tool_call=ToolCall(id=call_id, name="bash", arguments={"command": command}),
    )


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    logs = tmp_path / "logs"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(runner, "ModelClient", _FakeModelClient)
    monkeypatch.setenv("RHO_AGENT_LOGS_DIR", str(logs))
    monkeypatch.setenv("RHO_AGENT_MODEL", "gpt-6.1-sol")
    for name in (
        "RHO_AGENT_COST_CEILING_USD",
        "RHO_AGENT_MAX_WALL_SECONDS",
        "RHO_AGENT_ENABLE_REVIEWER",
        "RHO_AGENT_CONFIRM_DONE",
        "RHO_AGENT_CONFIRM_DONE_MAX",
        "RHO_AGENT_TEMPERATURE",
        "RHO_AGENT_REASONING_EFFORT",
        "RHO_AGENT_API_KEY",
        "RHO_AGENT_AUTO_COMPACT",
        "RHO_AGENT_VIEW_IMAGE",
        "RHO_AGENT_COMPACT_THRESHOLD",
        "RHO_AGENT_COMPACT_AT_TOKENS",
        "RHO_AGENT_REQUEST_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)
    _FakeModelClient.instances = []
    _FakeModelClient.priced = True
    return logs, work


async def test_runner_writes_trace_tokens_and_valid_atif(harness) -> None:
    logs, work = harness
    _FakeModelClient.script = [
        [_bash("a", "echo hello > out.txt && cat out.txt"), _done(0.10)],
        [StreamEvent(type="text", content="Wrote out.txt."), _done(0.10)],
        # completion gate round
        [_bash("b", "test -f out.txt && echo present"), _done(0.10)],
        [StreamEvent(type="text", content="CONFIRM_DONE"), _done(0.10)],
    ]

    await runner.run_task("write hello to out.txt", str(work), bash_only=True)

    assert (work / "out.txt").read_text() == "hello\n"
    client = _FakeModelClient.instances[0]
    assert client.kwargs["model"] == "gpt-6.1-sol"
    assert client.kwargs["timeout"] == 600.0  # reasoning can be silent for minutes
    assert [t["function"]["name"] for t in client.prompts[0].tools] == ["bash"]
    assert len(client.prompts) == 4

    tokens = json.loads((logs / "tokens.json").read_text())
    assert tokens["input"] == 400 and abs(tokens["cost_usd"] - 0.4) < 1e-9

    state = State.from_jsonl((logs / "trace.jsonl").read_bytes())
    users = [m["content"] for m in state.messages if m["role"] == "user"]
    assert users[0] == "write hello to out.txt"
    assert "CONFIRM_DONE" in users[1]  # the gate prompt
    assert state.status == "completed"

    trajectory = json.loads((logs / "trajectory.json").read_text())
    assert [s["source"] for s in trajectory["steps"]] == [
        "user",
        "agent",
        "agent",
        "user",
        "agent",
        "agent",
    ]
    assert trajectory["steps"][1]["tool_calls"][0]["function_name"] == "bash"
    assert "hello" in trajectory["steps"][1]["observation"]["results"][0]["content"]
    assert trajectory["final_metrics"]["total_prompt_tokens"] == 400

    harbor_models = pytest.importorskip("harbor.models.trajectories")
    harbor_models.Trajectory.model_validate(trajectory)


async def test_runner_stops_at_cost_ceiling_and_skips_the_gate(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs, work = harness
    monkeypatch.setenv("RHO_AGENT_COST_CEILING_USD", "0.5")
    _FakeModelClient.script = [[_bash(f"c{i}", "true"), _done(0.3)] for i in range(10)]

    await runner.run_task("loop forever", str(work), bash_only=True)

    client = _FakeModelClient.instances[0]
    assert len(client.prompts) == 2  # $0.30, then $0.60 >= ceiling; no gate round after
    state = State.from_jsonl((logs / "trace.jsonl").read_bytes())
    assert state.status == "budget_exceeded"
    assert state.pending_tool_call_ids() == []
    assert (logs / "trajectory.json").exists()


async def test_runner_without_gate_ends_on_first_answer(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _logs, work = harness
    monkeypatch.setenv("RHO_AGENT_CONFIRM_DONE", "0")
    _FakeModelClient.script = [[StreamEvent(type="text", content="done"), _done(0.1)]]

    await runner.run_task("say done", str(work), bash_only=True)

    assert len(_FakeModelClient.instances[0].prompts) == 1


async def test_runner_refuses_a_cost_ceiling_it_cannot_enforce(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _logs, work = harness
    monkeypatch.setenv("RHO_AGENT_COST_CEILING_USD", "4")
    _FakeModelClient.priced = False
    _FakeModelClient.script = [[StreamEvent(type="text", content="done"), _done(0.0)]]

    with pytest.raises(SystemExit) as exit_info:
        await runner.run_task("say done", str(work), bash_only=True)

    assert exit_info.value.code == 2
    assert _FakeModelClient.instances[0].prompts == []  # nothing was spent


async def test_runner_runs_an_unpriced_model_without_a_ceiling(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _logs, work = harness
    monkeypatch.setenv("RHO_AGENT_CONFIRM_DONE", "0")
    _FakeModelClient.priced = False
    _FakeModelClient.script = [[StreamEvent(type="text", content="done"), _done(0.0)]]

    await runner.run_task("say done", str(work), bash_only=True)

    assert len(_FakeModelClient.instances[0].prompts) == 1
