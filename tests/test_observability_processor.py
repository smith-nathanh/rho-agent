from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from rho_agent.core.agent import AgentEvent
from rho_agent.observability.config import CaptureConfig, ObservabilityConfig, TenantConfig
from rho_agent.observability.context import TelemetryContext, TurnContext, ToolExecutionContext
from rho_agent.observability.exporters.base import Exporter
from rho_agent.observability.processor import ObservabilityProcessor


class CapturingExporter(Exporter):
    def __init__(self) -> None:
        self.tool_executions: list[ToolExecutionContext] = []
        self.model_calls: list[tuple[str, int, int, int]] = []
        self.ended_turns: list[TurnContext] = []

    async def start_session(self, context: TelemetryContext) -> None:
        return None

    async def end_session(self, context: TelemetryContext) -> None:
        return None

    async def start_turn(self, turn: TurnContext, user_input: str = "") -> None:
        return None

    async def end_turn(self, turn: TurnContext) -> None:
        self.ended_turns.append(turn)

    async def record_model_call(
        self,
        turn_id: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
    ) -> None:
        self.model_calls.append((turn_id, input_tokens, output_tokens, latency_ms))

    async def record_tool_execution(self, execution: ToolExecutionContext) -> None:
        self.tool_executions.append(execution)

    async def increment_tool_call(self, session_id: str) -> None:
        return None


def _build_processor(exporter: CapturingExporter) -> ObservabilityProcessor:
    config = ObservabilityConfig(
        enabled=True,
        tenant=TenantConfig("team", "project"),
        capture=CaptureConfig(tool_results=True),
    )
    context = TelemetryContext.from_config(config, model="gpt-5-mini", profile="readonly")
    return ObservabilityProcessor(config, context, exporter=exporter)


@pytest.mark.asyncio
async def test_processor_pairs_tools_by_call_id() -> None:
    exporter = CapturingExporter()
    processor = _build_processor(exporter)

    async def events() -> AsyncIterator[AgentEvent]:
        yield AgentEvent(
            type="tool_start",
            tool_name="bash",
            tool_call_id="call_1",
            tool_args={"cmd": "echo one"},
        )
        yield AgentEvent(
            type="tool_start",
            tool_name="read",
            tool_call_id="call_2",
            tool_args={"path": "two.txt"},
        )
        yield AgentEvent(
            type="tool_end",
            tool_name="read",
            tool_call_id="call_2",
            tool_result="two",
        )
        yield AgentEvent(
            type="tool_end",
            tool_name="bash",
            tool_call_id="call_1",
            tool_result="one",
        )
        yield AgentEvent(
            type="turn_complete",
            usage={
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_reasoning_tokens": 0,
                "context_size": 0,
            },
        )

    observed = [event async for event in processor.wrap_turn(events(), "prompt")]
    assert len(observed) == 5
    assert processor.context.total_tool_calls == 2
    assert len(exporter.tool_executions) == 2

    read_exec = next(exec for exec in exporter.tool_executions if exec.tool_name == "read")
    bash_exec = next(exec for exec in exporter.tool_executions if exec.tool_name == "bash")
    assert read_exec.arguments == {"path": "two.txt"}
    assert read_exec.result == "two"
    assert bash_exec.arguments == {"cmd": "echo one"}
    assert bash_exec.result == "one"


@pytest.mark.asyncio
async def test_processor_records_api_usage_before_turn_complete() -> None:
    exporter = CapturingExporter()
    processor = _build_processor(exporter)

    async def events() -> AsyncIterator[AgentEvent]:
        yield AgentEvent(
            type="api_call_complete",
            usage={
                "input_tokens": 10,
                "output_tokens": 5,
                "reasoning_tokens": 2,
            },
        )
        yield AgentEvent(type="error", content="boom")

    observed = [event async for event in processor.wrap_turn(events(), "prompt")]
    assert len(observed) == 2
    assert processor.context.total_input_tokens == 10
    assert processor.context.total_output_tokens == 5
    assert processor.context.total_reasoning_tokens == 2
    assert len(exporter.model_calls) == 1
    assert len(exporter.ended_turns) == 1
    assert exporter.ended_turns[0].input_tokens == 10
    assert exporter.ended_turns[0].output_tokens == 5
    assert exporter.ended_turns[0].reasoning_tokens == 2
