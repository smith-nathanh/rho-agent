"""Normalize model discovery across OpenAI-compatible endpoints."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .pricing import Prices, table_prices


@dataclass(frozen=True)
class CatalogModel:
    id: str
    name: str = ""
    input_price: float | None = None
    output_price: float | None = None
    context_window: int | None = None
    supports_tools: bool = False


def _per_million(value: Any) -> float | None:
    try:
        price = float(value) * 1_000_000
    except (TypeError, ValueError, OverflowError):
        return None
    return price if math.isfinite(price) and price >= 0 else None


def parse_catalog(
    payload: Any,
    *,
    openrouter: bool,
    openai: bool,
    prices: Prices | None,
    service_tier: str | None,
) -> list[CatalogModel]:
    """Read model IDs and any usable price or capability metadata."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("The provider returned an invalid model catalog.")

    models: dict[str, CatalogModel] = {}
    for entry in payload["data"]:
        if not isinstance(entry, dict):
            continue
        model = entry.get("id")
        if not isinstance(model, str) or not model.strip() or not model.isprintable():
            continue
        name = entry.get("name")
        window = entry.get("context_length")
        parameters = entry.get("supported_parameters")
        rates = entry.get("pricing")
        rates = rates if isinstance(rates, dict) else {}

        fallback = prices
        if openai and fallback is None:
            fallback = table_prices(model, service_tier=service_tier)

        input_price = output_price = None
        if openrouter:
            input_price = _per_million(rates.get("prompt"))
            output_price = _per_million(rates.get("completion"))
        elif fallback is not None:
            input_price, output_price = fallback.input, fallback.output

        models[model] = CatalogModel(
            id=model,
            name=name if isinstance(name, str) and name.isprintable() else "",
            input_price=input_price,
            output_price=output_price,
            context_window=window if type(window) is int and window > 0 else None,
            supports_tools=isinstance(parameters, list) and "tools" in parameters,
        )
    return list(models.values())
