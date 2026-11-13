"""Cost of model calls: flat prices, LiteLLM's price list, and ModelClient."""

from __future__ import annotations

import warnings
from types import SimpleNamespace as NS
from typing import Any

import pytest

from rho_agent.client import pricing
from rho_agent.client.model import Message, ModelClient, Prompt
from rho_agent.client.pricing import Prices, call_cost, can_price, prices_from_env
from rho_agent.core.agent import Agent
from rho_agent.core.config import AgentConfig
from rho_agent.core.session import Session
from rho_agent.core.state import State

PRICE_ENVS = (
    "RHO_AGENT_PRICE_INPUT",
    "RHO_AGENT_PRICE_CACHED_INPUT",
    "RHO_AGENT_PRICE_OUTPUT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*PRICE_ENVS, "RHO_AGENT_BASE_URL", "RHO_AGENT_API_KEY", "RHO_AGENT_API"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def no_price_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricing, "_load_litellm", lambda: None)


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


def test_explicit_prices_win_over_the_price_list() -> None:
    prices = Prices(input=1.0, output=1.0)
    assert call_cost("gpt-6.1-sol", {"input_tokens": 1_000_000}, prices=prices) == 1.0


def test_without_a_price_list_cost_is_unknown(no_price_list: None) -> None:
    assert call_cost("gpt-6.1-sol", USAGE) is None
    assert not can_price("gpt-6.1-sol")
    assert can_price("gpt-6.1-sol", Prices(input=1.0, output=1.0))


# ---------------------------------------------------------------------------
# LiteLLM's price list
# ---------------------------------------------------------------------------


@pytest.fixture
def sol_prices() -> dict[str, Any]:
    litellm = pytest.importorskip("litellm")
    entry = litellm.model_cost.get("gpt-6.1-sol")
    if entry is None:
        pytest.skip("installed LiteLLM does not list gpt-6.1-sol")
    return entry


def test_price_list_charges_cached_input_and_output(sol_prices: dict[str, Any]) -> None:
    expected = (
        10_000 * sol_prices["input_cost_per_token"]
        + 90_000 * sol_prices["cache_read_input_token_cost"]
        + 1_000 * sol_prices["output_cost_per_token"]
    )
    assert call_cost("gpt-6.1-sol", USAGE) == pytest.approx(expected)


def test_price_list_applies_the_long_context_tier(sol_prices: dict[str, Any]) -> None:
    if "input_cost_per_token_above_272k_tokens" not in sol_prices:
        pytest.skip("no long-context tier in this price list")
    usage = {"input_tokens": 300_000, "output_tokens": 1_000}
    expected = (
        300_000 * sol_prices["input_cost_per_token_above_272k_tokens"]
        + 1_000 * sol_prices["output_cost_per_token_above_272k_tokens"]
    )
    assert call_cost("gpt-6.1-sol", usage) == pytest.approx(expected)


def test_price_list_applies_the_flex_tier(sol_prices: dict[str, Any]) -> None:
    usage = {"input_tokens": 100_000, "output_tokens": 1_000}
    expected = (
        100_000 * sol_prices["input_cost_per_token_flex"]
        + 1_000 * sol_prices["output_cost_per_token_flex"]
    )
    assert call_cost("gpt-6.1-sol", usage, service_tier="flex") == pytest.approx(expected)
    # "auto" and "default" are billed as standard.
    assert call_cost("gpt-6.1-sol", usage, service_tier="auto") == call_cost("gpt-6.1-sol", usage)


def test_unknown_models_have_no_price(sol_prices: dict[str, Any]) -> None:
    assert call_cost("no-such-model-xyz", USAGE) is None
    assert can_price("gpt-6.1-sol") and not can_price("no-such-model-xyz")


# ---------------------------------------------------------------------------
# ModelClient and Session
# ---------------------------------------------------------------------------


class _FakeResponses:
    def __init__(self, response: Any) -> None:
        self._response = response

    async def create(self, **kwargs: Any) -> Any:
        return self._response


def _response(cost: float | None = None) -> NS:
    usage = NS(
        input_tokens=USAGE["input_tokens"],
        output_tokens=USAGE["output_tokens"],
        input_tokens_details=NS(cached_tokens=USAGE["cached_tokens"]),
        output_tokens_details=NS(reasoning_tokens=0),
    )
    message = NS(type="message", id="m", phase=None, content=[NS(type="output_text", text="ok")])
    return NS(
        output=[message], status="completed", usage=usage, error=None, incomplete_details=None
    )


def _client(prices: Prices | None = None) -> ModelClient:
    client = ModelClient(model="gpt-6.1-sol", api_key="test-key", streaming=False, prices=prices)
    client._client = NS(responses=_FakeResponses(_response()))
    return client


PRICES = Prices(input=2.0, cached_input=0.1, output=10.0)
PROMPT = Prompt(system="s", messages=[Message(role="user", content="hi")], tools=[])


async def test_model_client_reports_cost_on_done_events(no_price_list: None) -> None:
    events = [event async for event in _client(PRICES).stream(PROMPT)]
    assert events[-1].usage["cost_usd"] == pytest.approx(PRICES.cost(USAGE))


async def test_model_client_prices_compaction_and_plain_completions(no_price_list: None) -> None:
    client = _client(PRICES)
    _, usage = await client.complete_prompt(PROMPT)
    assert usage["cost_usd"] == pytest.approx(PRICES.cost(USAGE))
    _, usage = await client.complete([{"role": "user", "content": "hi"}])
    assert usage["cost_usd"] == pytest.approx(PRICES.cost(USAGE))


async def test_model_client_reads_prices_from_the_environment(
    no_price_list: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RHO_AGENT_PRICE_INPUT", "2")
    monkeypatch.setenv("RHO_AGENT_PRICE_CACHED_INPUT", "0.1")
    monkeypatch.setenv("RHO_AGENT_PRICE_OUTPUT", "10")
    client = _client()
    assert client.can_price()
    _, usage = await client.complete_prompt(PROMPT)
    assert usage["cost_usd"] == pytest.approx(PRICES.cost(USAGE))


async def test_unpriced_model_reports_zero(no_price_list: None) -> None:
    client = _client()
    assert not client.can_price()
    events = [event async for event in client.stream(PROMPT)]
    assert events[-1].usage["cost_usd"] == 0.0


async def test_session_cost_cap_works_from_computed_cost(no_price_list: None) -> None:
    client = _client(PRICES)
    session = Session(Agent(AgentConfig(model="gpt-6.1-sol")), client=client, state=State())
    session.max_cost_usd = 0.01

    result = await session.run("hi")

    assert session.state.usage["cost_usd"] == pytest.approx(PRICES.cost(USAGE))
    assert result.text == "ok"


async def test_session_warns_when_a_cost_cap_cannot_work(no_price_list: None) -> None:
    session = Session(Agent(AgentConfig(model="gpt-6.1-sol")), client=_client(), state=State())
    session.max_cost_usd = 1.0

    with pytest.warns(RuntimeWarning, match="cost cap will never trigger"):
        await session.run("hi")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        await session.run("again")  # warned once per session
