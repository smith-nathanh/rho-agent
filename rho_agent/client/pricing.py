"""Dollar cost of a model call, computed from its token counts.

Providers report tokens, not dollars, so cost is tokens times price. Prices
come from, in order:

1. An explicit ``Prices`` (``ModelClient(prices=...)``), or the
   ``RHO_AGENT_PRICE_INPUT``, ``RHO_AGENT_PRICE_CACHED_INPUT`` and
   ``RHO_AGENT_PRICE_OUTPUT`` environment variables, in USD per million tokens.
2. LiteLLM's price list, when LiteLLM is installed (the ``evals`` extra). It
   knows cached-input prices, long-context price tiers and the flex and
   priority service tiers. Only the list bundled with the installed LiteLLM
   is used; nothing is fetched over the network.

With neither, the cost of a call is unknown and reported as 0.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from typing import Any

PRICE_INPUT_ENV = "RHO_AGENT_PRICE_INPUT"
PRICE_CACHED_INPUT_ENV = "RHO_AGENT_PRICE_CACHED_INPUT"
PRICE_OUTPUT_ENV = "RHO_AGENT_PRICE_OUTPUT"

# Service tiers LiteLLM prices separately; anything else is billed as standard.
_PRICED_SERVICE_TIERS = frozenset({"flex", "priority"})


@dataclass(frozen=True)
class Prices:
    """Flat prices in USD per million tokens. No long-context tiers."""

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


_litellm: Any = None
_litellm_loaded = False


def _load_litellm() -> Any:
    """Import LiteLLM once, using its bundled price list. None if not installed."""
    global _litellm, _litellm_loaded
    if not _litellm_loaded:
        _litellm_loaded = True
        # Without this, importing LiteLLM fetches the latest price list from
        # GitHub. The bundled one keeps cost reproducible for a given install.
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                import litellm
        except ImportError:
            litellm = None
        else:
            # Otherwise it prints a provider list for every unknown model.
            litellm.suppress_debug_info = True
        _litellm = litellm
    return _litellm


def litellm_cost(
    model: str, usage: dict[str, Any], service_tier: str | None = None
) -> float | None:
    """Cost from LiteLLM's price list, or None if LiteLLM or the model is unknown."""
    litellm = _load_litellm()
    if litellm is None:
        return None
    kwargs: dict[str, Any] = {
        "model": model,
        # LiteLLM counts cached tokens inside prompt_tokens, as the API does.
        "prompt_tokens": int(usage.get("input_tokens") or 0),
        "completion_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_input_tokens": int(usage.get("cached_tokens") or 0),
    }
    if service_tier in _PRICED_SERVICE_TIERS:
        kwargs["service_tier"] = service_tier
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            input_cost, output_cost = litellm.cost_per_token(**kwargs)
    except Exception:
        return None
    return float(input_cost) + float(output_cost)


def call_cost(
    model: str,
    usage: dict[str, Any],
    *,
    service_tier: str | None = None,
    prices: Prices | None = None,
) -> float | None:
    """Cost of one call in USD, or None when no price is known for ``model``."""
    if prices is not None:
        return prices.cost(usage)
    return litellm_cost(model, usage, service_tier)


def can_price(model: str, prices: Prices | None = None) -> bool:
    """Whether a price is known for ``model``."""
    probe = {"input_tokens": 1_000, "output_tokens": 1_000}
    return call_cost(model, probe, prices=prices) is not None
