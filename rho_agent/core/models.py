"""Per-model context limits used to decide when to compact.

Values are deliberately conservative: a window that is too small only makes
compaction fire earlier. Override per agent with ``AgentConfig.context_window``,
``AgentConfig.compact_threshold`` and ``AgentConfig.compact_at_tokens``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .events import AUTO_COMPACT_THRESHOLD

DEFAULT_CONTEXT_WINDOW = 128_000


@dataclass(frozen=True)
class ModelLimits:
    """Context limits for one model family."""

    context_window: int
    # Input size above which the provider bills at a higher rate, if any.
    # Compaction is kept below this so a long task never crosses into it.
    price_tier_tokens: int | None = None


# Matched in order against the model name with any provider prefix removed.
# First substring match wins, so list specific names before families.
_MODEL_LIMITS: tuple[tuple[str, ModelLimits], ...] = (
    # GPT-6.1 Sol: requests above 272K input tokens are reported to bill at 2x
    # input / 1.5x output. Source is third-party; confirm against OpenAI's
    # pricing page. The window itself is assumed, not confirmed.
    ("gpt-6.1-sol", ModelLimits(context_window=400_000, price_tier_tokens=272_000)),
    ("gpt-6", ModelLimits(context_window=400_000, price_tier_tokens=272_000)),
    ("gpt-5", ModelLimits(context_window=400_000)),
    ("gpt-oss", ModelLimits(context_window=128_000)),
    ("claude", ModelLimits(context_window=200_000)),
)


def limits_for(model: str | None) -> ModelLimits:
    """Return limits for a model name such as ``openai/gpt-5.2``."""
    name = (model or "").split("/")[-1].lower()
    for pattern, limits in _MODEL_LIMITS:
        if pattern in name:
            return limits
    return ModelLimits(context_window=DEFAULT_CONTEXT_WINDOW)


def validate_compaction_settings(
    threshold: float | None = None, compact_at_tokens: int | None = None
) -> None:
    """Raise ValueError for a compaction setting that cannot work."""
    if threshold is not None and not 0 < threshold <= 1:
        raise ValueError(
            f"compact_threshold must be a fraction above 0 and at most 1, got {threshold}"
        )
    if compact_at_tokens is not None and compact_at_tokens <= 0:
        raise ValueError(f"compact_at_tokens must be positive, got {compact_at_tokens}")


def compact_threshold(
    model: str | None,
    *,
    context_window: int | None = None,
    compact_at_tokens: int | None = None,
    threshold: float | None = None,
) -> int:
    """Input-token count above which the session should compact.

    Precedence:
    1. ``compact_at_tokens``: an exact token count, used as given.
    2. ``threshold``: a fraction of the context window, used as given.
    3. Default: ``AUTO_COMPACT_THRESHOLD`` of the window, capped at 90% of the
       model's price tier when it has one.
    """
    validate_compaction_settings(threshold, compact_at_tokens)
    if compact_at_tokens is not None:
        return compact_at_tokens
    limits = limits_for(model)
    window = context_window or limits.context_window
    if threshold is not None:
        return max(int(window * threshold), 1)
    threshold = int(window * AUTO_COMPACT_THRESHOLD)
    if limits.price_tier_tokens is not None:
        threshold = min(threshold, int(limits.price_tier_tokens * 0.9))
    return threshold
