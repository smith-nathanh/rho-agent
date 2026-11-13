"""Cost of model calls: flat prices, the OpenAI table, OpenRouter's reported cost."""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import AsyncMock

import pytest

from rho_agent.client import pricing
from rho_agent.client.model import Message, ModelClient, Prompt, reported_cost
from rho_agent.client.pricing import Prices, call_cost, can_price, prices_from_env, table_prices
from rho_agent.core.agent import Agent
from rho_agent.core.config import AgentConfig
from rho_agent.core.session import Session
from rho_agent.core.state import State

OPENROUTER = "https://openrouter.ai/api/v1"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "RHO_AGENT_PRICE_INPUT",
        "RHO_AGENT_PRICE_CACHED_INPUT",
        "RHO_AGENT_PRICE_OUTPUT",
        "RHO_AGENT_BASE_URL",
        "RHO_AGENT_API_KEY",
        "RHO_AGENT_API",
    ):
        monkeypatch.delenv(name, raising=False)


USAGE = {"input_tokens": 100_000, "cached_tokens": 90_000, "output_tokens": 1_000}


# ---------------------------------------------------------------------------
# Flat prices
# ---------------------------------------------------------------------------


def test_flat_prices_charge_cached_input_at_its_own_rate() -> None:
    prices = Prices(input=2.0, cached_input=0.1, output=10.0)
    # 10K uncached at $2/M + 90K cached at $0.10/M + 1K output at $10/M
    assert prices.cost(USAGE) == pytest.approx(0.02 + 0.009 + 0.01)


def test_cached_input_defaults_to_the_input_price() -> None:
    assert Prices(input=2.0, output=10.0).cost(USAGE) == pytest.approx(0.2 + 0.01)


def test_prices_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert prices_from_env() is None
    monkeypatch.setenv("RHO_AGENT_PRICE_INPUT", "2")
    with pytest.raises(ValueError, match="both"):
        prices_from_env()
    monkeypatch.setenv("RHO_AGENT_PRICE_OUTPUT", "10")
    monkeypatch.setenv("RHO_AGENT_PRICE_CACHED_INPUT", "0.1")
    assert prices_from_env() == Prices(input=2.0, output=10.0, cached_input=0.1)
    monkeypatch.setenv("RHO_AGENT_PRICE_OUTPUT", "ten")
    with pytest.raises(ValueError, match="numbers"):
        prices_from_env()


def test_explicit_prices_win_over_the_table() -> None:
    prices = Prices(input=1.0, output=1.0)
    assert call_cost("gpt-6.1-sol", {"input_tokens": 1_000_000}, prices=prices) == 1.0


# ---------------------------------------------------------------------------
# The built-in OpenAI table
# ---------------------------------------------------------------------------


def test_table_has_sol_with_cached_flex_priority_and_long_context() -> None:
    assert table_prices("gpt-6.1-sol") == Prices(input=2.0, cached_input=0.1, output=10.0)
    assert table_prices("gpt-6.1-sol", service_tier="flex") == Prices(
        input=1.0, cached_input=0.05, output=5.0
    )
    assert table_prices("gpt-6.1-sol", service_tier="priority").input == 4.0
    # "auto" and "default" are billed as standard.
    assert table_prices("gpt-6.1-sol", service_tier="auto") == table_prices("gpt-6.1-sol")
    # Above 272K input tokens the whole call is billed at the higher prices.
    assert table_prices("gpt-6.1-sol", input_tokens=272_000).input == 2.0
    assert table_prices("gpt-6.1-sol", input_tokens=272_001) == Prices(
        input=4.0, cached_input=0.2, output=15.0
    )


def test_table_cost_of_a_call() -> None:
    assert call_cost("gpt-6.1-sol", USAGE) == pytest.approx(0.02 + 0.009 + 0.01)
    long_call = {"input_tokens": 300_000, "output_tokens": 1_000}
    assert call_cost("gpt-6.1-sol", long_call) == pytest.approx(1.2 + 0.015)


def test_openai_prefix_and_unknown_models() -> None:
    assert table_prices("openai/gpt-6.1-sol") == table_prices("gpt-6.1-sol")
    assert call_cost("no-such-model", USAGE) is None
    assert can_price("gpt-6.1-sol") and not can_price("no-such-model")
    assert can_price("no-such-model", Prices(input=1.0, output=1.0))


def test_table_is_valid_and_regenerable() -> None:
    path = Path(pricing.__file__).with_name("openai_prices.json")
    table = json.loads(path.read_text())
    assert table["units"] == "USD per million tokens"
    for name, entry in table["models"].items():
        assert {"input", "output"} <= entry["standard"].keys(), name
        if "long_context" in entry:
            assert entry["long_context"]["above_tokens"] > 0, name


# ---------------------------------------------------------------------------
# Cost reported by the server (OpenRouter)
# ---------------------------------------------------------------------------


def test_reported_cost() -> None:
    assert reported_cost(NS(input_tokens=1)) is None  # OpenAI: no cost field
    assert reported_cost(NS(cost=0.0123, is_byok=False)) == 0.0123
    # With your own provider key, the provider's charge is added to OpenRouter's fee.
    byok = NS(cost=0.001, is_byok=True, cost_details={"upstream_inference_cost": 0.02})
    assert reported_cost(byok) == pytest.approx(0.021)


# ---------------------------------------------------------------------------
# ModelClient and Session
# ---------------------------------------------------------------------------


class _FakeResponses:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        return self._response


def _response(**usage_extra: Any) -> NS:
    usage = NS(
        input_tokens=USAGE["input_tokens"],
        output_tokens=USAGE["output_tokens"],
        input_tokens_details=NS(cached_tokens=USAGE["cached_tokens"]),
        output_tokens_details=NS(reasoning_tokens=0),
        **usage_extra,
    )
    message = NS(type="message", id="m", phase=None, content=[NS(type="output_text", text="ok")])
    return NS(
        output=[message], status="completed", usage=usage, error=None, incomplete_details=None
    )


def _client(
    model: str = "gpt-6.1-sol", base_url: str | None = None, **kwargs: Any
) -> tuple[ModelClient, _FakeResponses]:
    client = ModelClient(
        model=model, base_url=base_url, api_key="test-key", streaming=False, **kwargs
    )
    responses = _FakeResponses(_response())
    client._client = NS(responses=responses)
    return client, responses


PROMPT = Prompt(
    system="s", messages=[Message(role="user", content="hi")], tools=[], session_id="sess-1"
)
SOL_COST = 0.02 + 0.009 + 0.01


async def test_openai_calls_are_priced_from_the_table() -> None:
    client, _ = _client()
    events = [event async for event in client.stream(PROMPT)]
    assert events[-1].usage["cost_usd"] == pytest.approx(SOL_COST)
    _, usage = await client.complete_prompt(PROMPT)
    assert usage["cost_usd"] == pytest.approx(SOL_COST)
    _, usage = await client.complete([{"role": "user", "content": "hi"}])
    assert usage["cost_usd"] == pytest.approx(SOL_COST)


async def test_flex_service_tier_is_priced_as_flex() -> None:
    client, _ = _client(service_tier="flex")
    _, usage = await client.complete_prompt(PROMPT)
    assert usage["cost_usd"] == pytest.approx(SOL_COST / 2)


async def test_prices_from_the_environment_win(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RHO_AGENT_PRICE_INPUT", "1")
    monkeypatch.setenv("RHO_AGENT_PRICE_OUTPUT", "1")
    client, _ = _client(model="my-local-model", base_url="http://localhost:8000/v1")
    assert client.can_price()
    _, usage = await client.complete_prompt(PROMPT)
    assert usage["cost_usd"] == pytest.approx(0.101)


async def test_openrouter_cost_is_taken_from_the_response() -> None:
    client, _ = _client(model="openai/gpt-6.1-sol", base_url=OPENROUTER)
    client._client = NS(responses=_FakeResponses(_response(cost=0.0042)))
    assert client.can_price()  # OpenRouter reports cost, whatever the model
    events = [event async for event in client.stream(PROMPT)]
    assert events[-1].usage["cost_usd"] == 0.0042


async def test_openrouter_zero_cost_is_kept() -> None:
    client, _ = _client(model="openai/gpt-6.1-sol", base_url=OPENROUTER)
    client._client = NS(responses=_FakeResponses(_response(cost=0)))
    _, usage = await client.complete_prompt(PROMPT)
    assert usage["cost_usd"] == 0.0  # a free model, not an unknown price


async def test_session_id_is_sent_to_openrouter_only() -> None:
    client, _ = _client(model="openai/gpt-6.1-sol", base_url=OPENROUTER)
    fake = _FakeResponses(_response(cost=0.001))
    client._client = NS(responses=fake)
    await client.complete_prompt(PROMPT)
    assert fake.requests[-1]["extra_headers"] == {"x-session-id": "sess-1"}

    openai_client, openai_fake = _client()
    await openai_client.complete_prompt(PROMPT)
    assert "extra_headers" not in openai_fake.requests[-1]


async def test_openrouter_chat_fallback_reads_cost_and_sends_session_id() -> None:
    client = ModelClient(
        model="some/model", base_url=OPENROUTER, api_key="k", api="chat", streaming=False
    )
    usage = NS(
        prompt_tokens=10,
        completion_tokens=5,
        prompt_tokens_details=NS(cached_tokens=4),
        completion_tokens_details=NS(reasoning_tokens=2),
        cost=0.0007,
    )
    reply = NS(choices=[NS(message=NS(content="sum", tool_calls=None))], usage=usage)
    create = AsyncMock(return_value=reply)
    client._chat._client = NS(chat=NS(completions=NS(create=create)))

    text, result = await client.complete_prompt(PROMPT)

    assert text == "sum"
    assert result == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cached_tokens": 4,
        "reasoning_tokens": 2,
        "cost_usd": 0.0007,
    }
    assert create.call_args.kwargs["extra_headers"] == {"x-session-id": "sess-1"}


async def test_unpriced_model_reports_zero() -> None:
    client, _ = _client(model="my-local-model", base_url="http://localhost:8000/v1")
    assert not client.can_price()
    events = [event async for event in client.stream(PROMPT)]
    assert events[-1].usage["cost_usd"] == 0.0


async def test_session_tracks_cost_from_the_table() -> None:
    client, _ = _client()
    session = Session(Agent(AgentConfig(model="gpt-6.1-sol")), client=client, state=State())
    session.max_cost_usd = 0.01

    result = await session.run("hi")

    assert result.text == "ok"
    assert session.state.usage["cost_usd"] == pytest.approx(SOL_COST)


async def test_session_sends_its_own_id_to_openrouter() -> None:
    client, _ = _client(model="openai/gpt-6.1-sol", base_url=OPENROUTER)
    fake = _FakeResponses(_response(cost=0.002))
    client._client = NS(responses=fake)
    session = Session(Agent(AgentConfig(model="openai/gpt-6.1-sol")), client=client, state=State())

    await session.run("hi")

    assert fake.requests[0]["extra_headers"] == {"x-session-id": session.id}
    assert session.state.usage["cost_usd"] == 0.002


async def test_session_warns_when_a_cost_cap_cannot_work() -> None:
    client, _ = _client(model="my-local-model", base_url="http://localhost:8000/v1")
    session = Session(Agent(AgentConfig(model="my-local-model")), client=client, state=State())
    session.max_cost_usd = 1.0

    with pytest.warns(RuntimeWarning, match="cost cap will never trigger"):
        await session.run("hi")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        await session.run("again")  # warned once per session
