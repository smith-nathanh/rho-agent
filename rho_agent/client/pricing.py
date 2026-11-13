"""Dollar cost of a model call.

OpenAI reports tokens, not dollars, so cost is tokens times price. OpenRouter
reports the cost of each call itself, and ``ModelClient`` uses that when it is
there. Otherwise prices come from, in order:

1. An explicit ``Prices`` (``ModelClient(prices=...)``), or the
   ``RHO_AGENT_PRICE_INPUT``, ``RHO_AGENT_PRICE_CACHED_INPUT`` and
   ``RHO_AGENT_PRICE_OUTPUT`` environment variables, in USD per million tokens.
2. The built-in OpenAI price table, ``openai_prices.json``. It has standard,
   flex and priority prices and the higher prices for long prompts, and is
   regenerated from LiteLLM's price list with ``scripts/update_prices.py``.

With neither, the cost of a call is unknown and reported as 0.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Any

PRICE_INPUT_ENV = "RHO_AGENT_PRICE_INPUT"
PRICE_CACHED_INPUT_ENV = "RHO_AGENT_PRICE_CACHED_INPUT"
PRICE_OUTPUT_ENV = "RHO_AGENT_PRICE_OUTPUT"

# Service tiers priced separately; anything else ("auto", "default") is standard.
_SERVICE_TIERS = frozenset({"flex", "priority"})


@dataclass(frozen=True)
class Prices:
    """Flat prices in USD per million tokens."""

    input: float
    output: float
    # Price of input tokens served from the prompt cache; defaults to ``input``.
    cached_input: float | None = None

    def cost(self, usage: dict[str, Any]) -> float:
        input_tokens = int(usage.get("input_tokens") or 0)
        cached = min(int(usage.get("cached_tokens") or 0), input_tokens)
        output_tokens = int(usage.get("output_tokens") or 0)
        cached_price = self.input if self.cached_input is None else self.cached_input
        return (
            (input_tokens - cached) * self.input
            + cached * cached_price
            + output_tokens * self.output
        ) / 1_000_000


def prices_from_env() -> Prices | None:
    """Prices from the environment, or None when input and output are not both set."""
    input_price = os.getenv(PRICE_INPUT_ENV)
    output_price = os.getenv(PRICE_OUTPUT_ENV)
    if not input_price and not output_price:
        return None
    if not (input_price and output_price):
        raise ValueError(f"Set both {PRICE_INPUT_ENV} and {PRICE_OUTPUT_ENV}, or neither.")
    cached = os.getenv(PRICE_CACHED_INPUT_ENV)
    try:
        return Prices(
            input=float(input_price),
            output=float(output_price),
            cached_input=float(cached) if cached else None,
        )
    except ValueError as e:
        raise ValueError(f"Prices must be numbers (USD per million tokens): {e}") from e


@cache
def _openai_table() -> dict[str, Any]:
    text = files("rho_agent.client").joinpath("openai_prices.json").read_text(encoding="utf-8")
    return json.loads(text)["models"]


def table_prices(
    model: str, input_tokens: int = 0, service_tier: str | None = None
) -> Prices | None:
    """Prices for an OpenAI model from the built-in table, or None if it is not listed.

    A prompt longer than the model's long-context threshold is billed at the
    long-context prices for the whole call, input and output.
    """
    entry = _openai_table().get(model.removeprefix("openai/"))
    if entry is None:
        return None
    tier = service_tier if service_tier in _SERVICE_TIERS else "standard"
    prices = entry.get(tier) or entry["standard"]
    long_context = entry.get("long_context")
    if long_context and input_tokens > long_context["above_tokens"]:
        prices = long_context.get(tier) or long_context["standard"]
    return Prices(**prices)


def call_cost(
    model: str,
    usage: dict[str, Any],
    *,
    service_tier: str | None = None,
    prices: Prices | None = None,
) -> float | None:
    """Cost of one call in USD, or None when no price is known for ``model``."""
    if prices is None:
        input_tokens = int(usage.get("input_tokens") or 0)
        prices = table_prices(model, input_tokens, service_tier)
    return None if prices is None else prices.cost(usage)


def can_price(model: str, prices: Prices | None = None) -> bool:
    """Whether a price is known for ``model``."""
    return prices is not None or table_prices(model) is not None
