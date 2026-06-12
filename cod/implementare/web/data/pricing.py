"""Per-model pricing — open-source / local models are free."""
from __future__ import annotations

import re

# USD per 1M tokens. (input, output, cache_read).
# All open-source / local providers default to (0, 0, 0).
_PRICES: dict[str, tuple[float, float, float]] = {
    # OpenAI
    "gpt-4o": (2.5, 10.0, 1.25),
    "gpt-4o-mini": (0.15, 0.60, 0.075),
    "gpt-4.1": (2.0, 8.0, 0.5),
    "gpt-4.1-mini": (0.4, 1.6, 0.1),
    "gpt-4.1-nano": (0.1, 0.4, 0.025),
    "o1": (15.0, 60.0, 7.5),
    "o1-mini": (3.0, 12.0, 1.5),
    "o3": (10.0, 40.0, 2.5),
    "o3-mini": (1.1, 4.4, 0.55),
    # Anthropic
    "claude-opus-4-7": (15.0, 75.0, 1.5),
    "claude-opus-4-6": (15.0, 75.0, 1.5),
    "claude-opus-4": (15.0, 75.0, 1.5),
    "claude-sonnet-4-6": (3.0, 15.0, 0.3),
    "claude-sonnet-4-5": (3.0, 15.0, 0.3),
    "claude-sonnet-4": (3.0, 15.0, 0.3),
    "claude-haiku-4-5": (1.0, 5.0, 0.1),
    "claude-3-5-sonnet": (3.0, 15.0, 0.3),
    "claude-3-5-haiku": (0.8, 4.0, 0.08),
    # Google
    "gemini-2.5-pro": (1.25, 10.0, 0.31),
    "gemini-2.5-flash": (0.3, 2.5, 0.075),
    "gemini-2.0-flash": (0.1, 0.4, 0.025),
    "gemini-1.5-pro": (1.25, 5.0, 0.31),
    "gemini-1.5-flash": (0.075, 0.3, 0.019),
}

# Providers whose models are always free (local / open source).
_FREE_PROVIDERS = {"ollama", "local", "llamacpp", "lmstudio", "vllm", "tgi"}


def is_free_provider(provider: str) -> bool:
    return (provider or "").lower() in _FREE_PROVIDERS


def _normalize_model_id(model: str) -> str:
    """Strip cross-region + vendor prefixes from Bedrock-style IDs.

    Bedrock IDs look like `us.anthropic.claude-opus-4-1-20250805-v1:0`. The
    price table is keyed on the bare model family (`claude-opus-4-1`), so we
    drop the leading `<region>.<vendor>.` segments and the trailing
    `-v<N>[:N]` revision suffix.
    """
    key = model.lower()
    # Drop trailing `:0` provisioned-throughput suffix.
    key = key.split(":", 1)[0]
    # Drop `<region>.<vendor>.` or `<vendor>.` prefix (e.g. `us.anthropic.`).
    parts = key.split(".")
    if len(parts) >= 2 and parts[-2] in {"anthropic", "amazon", "meta", "mistral", "cohere", "ai21"}:
        key = parts[-1]
    # Drop trailing `-vN` revision suffix.
    key = re.sub(r"-v\d+$", "", key)
    return key


def model_price(provider: str, model: str) -> tuple[float, float, float]:
    """Return (input, output, cache_read) USD-per-1M-tokens. Zero if unknown/free."""
    if is_free_provider(provider):
        return (0.0, 0.0, 0.0)
    if not model:
        return (0.0, 0.0, 0.0)
    key = _normalize_model_id(model)
    if key in _PRICES:
        return _PRICES[key]
    # Prefix match for versioned ids ("claude-sonnet-4-6-20250101").
    # Longest key first so "claude-opus-4-7" wins over "claude-opus-4".
    for k in sorted(_PRICES, key=len, reverse=True):
        if key.startswith(k):
            return _PRICES[k]
    return (0.0, 0.0, 0.0)


def compute_cost(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> dict[str, float]:
    p_in, p_out, p_cache = model_price(provider, model)
    inp = input_tokens * p_in / 1_000_000
    out = output_tokens * p_out / 1_000_000
    cache = cache_read_tokens * p_cache / 1_000_000
    return {
        "input": round(inp, 6),
        "output": round(out, 6),
        "cache_read": round(cache, 6),
        "total": round(inp + out + cache, 6),
        "free": is_free_provider(provider) or (p_in == 0 and p_out == 0),
    }
