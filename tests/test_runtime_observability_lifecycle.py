from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from rho_agent.core.agent import AgentEvent
from rho_agent.observability.config import ObservabilityConfig, TenantConfig
from rho_agent.observability.context import TelemetryContext
from rho_agent.observability.exporters.base import Exporter
from rho_agent.observability.processor import ObservabilityProcessor
from rho_agent.runtime.options import RuntimeOptions
from rho_agent.runtime.run import run_prompt
from rho_agent.runtime.types import LocalRuntime


class CountingExporter(Exporter):
    def __init__(self) -> None:
        self.start_session_calls = 0
        self.end_session_calls = 0

    async def start_session(self, context: TelemetryContext) -> None:
        self.start_session_calls += 1
        if self.start_session_calls > 1:
            raise AssertionError("start_session called more than once")

    async def end_session(self, context: TelemetryContext) -> None:
        self.end_session_calls += 1

    async def start_turn(self, turn, user_input: str = "") -> None:  # type: ignore[override]
        return None

    async def end_turn(self, turn) -> None:  # type: ignore[override]
        return None

    async def record_model_call(
        self,
        turn_id: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
    ) -> None:
        return None

    async def record_tool_execution(self, execution) -> None:  # type: ignore[override]
        return None


class DummyAgent:
    def __init__(self) -> None:
        self.turn_count = 0

    async def run_turn(self, user_input: str) -> AsyncIterator[AgentEvent]:
        self.turn_count += 1
        yield AgentEvent(type="text", content=f"reply-{self.turn_count}")
        yield AgentEvent(
            type="turn_complete",
            usage={
                "total_input_tokens": self.turn_count,
                "total_output_tokens": self.turn_count,
                "total_reasoning_tokens": 0,
                "context_size": self.turn_count,
            },
        )


def _build_runtime(exporter: CountingExporter) -> LocalRuntime:
    config = ObservabilityConfig(enabled=True, tenant=TenantConfig("team", "project"))
    context = TelemetryContext.from_config(config, model="gpt-5-mini", profile="readonly")
    processor = ObservabilityProcessor(config, context, exporter=exporter)
    return LocalRuntime(
        agent=DummyAgent(),  # type: ignore[arg-type]
        session=object(),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        model="gpt-5-mini",
        profile_name="readonly",
        session_id=context.session_id,
        options=RuntimeOptions(),
        observability=processor,
    )


@pytest.mark.asyncio
async def test_run_prompt_does_not_manage_runtime_session_lifecycle() -> None:
    exporter = CountingExporter()
    runtime = _build_runtime(exporter)

    first = await run_prompt(runtime, "first")
    second = await run_prompt(runtime, "second")

    assert first.status == "completed"
    assert second.status == "completed"
    assert exporter.start_session_calls == 0
    assert exporter.end_session_calls == 0


@pytest.mark.asyncio
async def test_start_and_close_runtime_are_idempotent() -> None:
    exporter = CountingExporter()
    runtime = _build_runtime(exporter)

    await runtime.start()
    await runtime.start()

    await run_prompt(runtime, "first")
    await run_prompt(runtime, "second")

    await runtime.close("completed")
    await runtime.close("completed")

    assert exporter.start_session_calls == 1
    assert exporter.end_session_calls == 1
