"""Regenerate rho-agent's built-in OpenAI price table from LiteLLM's price list.

    python scripts/update_prices.py                  # fetch LiteLLM's latest list
    python scripts/update_prices.py path/to/file.json

Writes ``rho_agent/client/openai_prices.json`` and prints what changed. Only
OpenAI chat and Responses models are kept, with standard, flex and priority
prices and the higher prices above a long-context threshold. Standard library
only, so it runs without installing anything.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

SOURCE_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
TARGET = Path(__file__).resolve().parent.parent / "rho_agent" / "client" / "openai_prices.json"

SERVICE_TIERS = {"standard": "", "flex": "_flex", "priority": "_priority"}
_ABOVE = re.compile(r"^input_cost_per_token_above_(\d+)k_tokens$")


def _per_million(value: object) -> float | None:
    if not isinstance(value, int | float):
        return None
    return round(float(value) * 1_000_000, 6)


def _prices(entry: dict, infix: str, suffix: str) -> dict | None:
    """Input, cached-input and output prices for one tier, or None if absent."""
    found = {
        "input": _per_million(entry.get(f"input_cost_per_token{infix}{suffix}")),
        "cached_input": _per_million(entry.get(f"cache_read_input_token_cost{infix}{suffix}")),
        "output": _per_million(entry.get(f"output_cost_per_token{infix}{suffix}")),
    }
    if found["input"] is None or found["output"] is None:
        return None
    if found["cached_input"] is None:
        del found["cached_input"]
    return found


def _model(entry: dict) -> dict | None:
    model: dict = {}
    for tier, suffix in SERVICE_TIERS.items():
        prices = _prices(entry, "", suffix)
        if prices:
            model[tier] = prices
    if "standard" not in model:
        return None
    thresholds = sorted(int(m.group(1)) for k in entry if (m := _ABOVE.match(k)))
    if thresholds:
        size = thresholds[0]
        long_context: dict = {"above_tokens": size * 1000}
        for tier, suffix in SERVICE_TIERS.items():
            prices = _prices(entry, f"_above_{size}k_tokens", suffix)
            if prices:
                long_context[tier] = prices
        if "standard" in long_context:
            model["long_context"] = long_context
    return model


def build(source: dict) -> dict[str, dict]:
    models = {}
    for name, entry in source.items():
        if not isinstance(entry, dict) or entry.get("litellm_provider") != "openai":
            continue
        if entry.get("mode") not in ("chat", "responses") or name.startswith("ft:"):
            continue
        model = _model(entry)
        if model:
            models[name] = model
    return dict(sorted(models.items()))


def main() -> None:
    if len(sys.argv) > 1:
        source = json.loads(Path(sys.argv[1]).read_text())
        origin = sys.argv[1]
    else:
        with urllib.request.urlopen(SOURCE_URL, timeout=30) as response:
            source = json.load(response)
        origin = SOURCE_URL
    models = build(source)

    old = json.loads(TARGET.read_text())["models"] if TARGET.exists() else {}
    added = sorted(set(models) - set(old))
    removed = sorted(set(old) - set(models))
    changed = sorted(n for n in set(models) & set(old) if models[n] != old[n])

    TARGET.write_text(
        json.dumps(
            {
                "source": origin,
                "generated": datetime.now(UTC).date().isoformat(),
                "units": "USD per million tokens",
                "models": models,
            },
            indent=1,
        )
        + "\n"
    )
    print(f"{len(models)} models written to {TARGET}")
    for label, names in (("added", added), ("removed", removed), ("changed", changed)):
        if names:
            print(f"  {label}: {', '.join(names)}")


if __name__ == "__main__":
    main()
