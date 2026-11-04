"""
Token pricing helpers.

Costs are computed from stored token counts at read time; they are never
written back into ``usage_events`` (see C1-f3). Prices are USD per 1M tokens.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

# USD per 1M tokens. Cache-hit input is far cheaper than cache-miss input.
PRICES: Dict[str, Dict[str, float]] = {
    "deepseek-chat": {"input_cache_hit": 0.028, "input": 0.28, "output": 0.42},
    "deepseek-reasoner": {"input_cache_hit": 0.14, "input": 0.55, "output": 2.19},
    "gpt-4o": {"input_cache_hit": 1.25, "input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input_cache_hit": 0.075, "input": 0.15, "output": 0.60},
    "gpt-4.1": {"input_cache_hit": 1.00, "input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input_cache_hit": 0.20, "input": 0.40, "output": 1.60},
    "o3-mini": {"input_cache_hit": 0.55, "input": 1.10, "output": 4.40},
    "claude-3-5-sonnet": {"input_cache_hit": 0.30, "input": 3.00, "output": 15.00},
    "claude-3-7-sonnet": {"input_cache_hit": 0.30, "input": 3.00, "output": 15.00},
    "claude-sonnet-4": {"input_cache_hit": 0.30, "input": 3.00, "output": 15.00},
    "claude-opus-4": {"input_cache_hit": 1.50, "input": 15.00, "output": 75.00},
    "gemini-2.0-flash": {"input_cache_hit": 0.025, "input": 0.10, "output": 0.40},
    "gemini-2.5-pro": {"input_cache_hit": 0.31, "input": 1.25, "output": 10.00},
    "qwen-max": {"input_cache_hit": 0.24, "input": 1.20, "output": 6.00},
}

_RATE_KEYS = ("input_cache_hit", "input", "output")


def _highest_rates() -> Dict[str, float]:
    """The most expensive value of each rate in the table.

    Using the maximum per component means an unknown model is never
    under-reported, even when the priciest model has an unusual mix.
    """
    return {key: max(price[key] for price in PRICES.values()) for key in _RATE_KEYS}


HIGHEST_RATES = _highest_rates()


def _normalise(model: Any) -> str:
    return str(model or "").strip().lower()


def get_price(model: Any) -> Tuple[Dict[str, float], bool]:
    """Return ``(rates, known)`` for a model name.

    Exact match wins, then the longest matching prefix (so
    ``deepseek-chat-v2`` uses ``deepseek-chat``). Unknown models are priced at
    the highest rate in the table and reported as unknown.
    """
    name = _normalise(model)
    if name in PRICES:
        return PRICES[name], True
    for key in sorted(PRICES, key=len, reverse=True):
        if name.startswith(key):
            return PRICES[key], True
    return HIGHEST_RATES, False


def pricing_known(model: Any) -> bool:
    return get_price(model)[1]


def cost_from_rates(
    rates: Dict[str, float],
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
) -> float:
    """Compute USD cost; token counts are clamped to sane non-negative values."""
    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    cached = max(0, int(cached_tokens or 0))
    uncached = max(0, prompt - cached)
    return (
        cached * float(rates["input_cache_hit"])
        + uncached * float(rates["input"])
        + completion * float(rates["output"])
    ) / 1_000_000.0


def compute_cost(
    model: Any,
    prompt_tokens: int,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
) -> float:
    """Cost in USD for one usage event."""
    rates, _ = get_price(model)
    return cost_from_rates(rates, prompt_tokens, completion_tokens, cached_tokens)


def events_cost(events: Iterable[Dict[str, Any]]) -> float:
    """Total cost of usage-event dictionaries (rows from ``usage_events``)."""
    return sum(
        compute_cost(
            event.get("model"),
            event.get("prompt_tokens") or 0,
            event.get("completion_tokens") or 0,
            event.get("cached_tokens") or 0,
        )
        for event in events
    )
