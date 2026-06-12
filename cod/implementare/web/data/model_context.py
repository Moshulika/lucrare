"""Per-model context-window + max-output-token lookup.

Mirrors the shape of `pricing.py` — keyed on a normalized model family with
prefix-match fallback for versioned IDs. Used by the web dashboard's
context-window pie chart when the session itself has no recorded
`metrics.context.limit` (older sessions, sessions created before
context_limit plumbing existed).

Numbers are the published vendor limits as of late-2025. Open-source / local
models default to 32k since per-model context length is configurable at
serve time and we can't introspect it from a static table.
"""
from __future__ import annotations

import re

# (max_context_tokens, max_output_tokens)
_CONTEXT: dict[str, tuple[int, int]] = {
    # ----- OpenAI -----
    "gpt-4o": (128_000, 16_384),
    "gpt-4o-mini": (128_000, 16_384),
    "gpt-4.1": (1_000_000, 32_768),
    "gpt-4.1-mini": (1_000_000, 32_768),
    "gpt-4.1-nano": (1_000_000, 32_768),
    "gpt-5": (400_000, 128_000),
    "gpt-5.4": (400_000, 128_000),
    "gpt-5.5": (400_000, 128_000),
    "o1": (200_000, 100_000),
    "o1-mini": (128_000, 65_536),
    "o3": (200_000, 100_000),
    "o3-mini": (200_000, 100_000),
    # ----- Anthropic (Claude 4 family is 200k; opt-in 1M beta exists) -----
    "claude-opus-4-7": (200_000, 32_000),
    "claude-opus-4-6": (200_000, 32_000),
    "claude-opus-4-1": (200_000, 32_000),
    "claude-opus-4": (200_000, 32_000),
    "claude-sonnet-4-6": (200_000, 64_000),
    "claude-sonnet-4-5": (200_000, 64_000),
    "claude-sonnet-4": (200_000, 64_000),
    "claude-haiku-4-5": (200_000, 32_000),
    "claude-3-5-sonnet": (200_000, 8_192),
    "claude-3-5-haiku": (200_000, 8_192),
    # ----- Google Gemini -----
    "gemini-2.5-pro": (1_048_576, 65_536),
    "gemini-2.5-flash": (1_048_576, 65_536),
    "gemini-2.0-flash": (1_048_576, 8_192),
    "gemini-1.5-pro": (2_097_152, 8_192),
    "gemini-1.5-flash": (1_048_576, 8_192),
}

# Fallback for local / open-source models — Ollama's default context is 2k but
# most modern instruct models negotiate up to 8-128k. 32k is a reasonable
# middle ground for the dashboard; the chart already degrades gracefully if
# usage exceeds the displayed limit (`over_limit` slice).
_LOCAL_DEFAULT = (32_000, 4_096)
_LOCAL_PROVIDERS = {"ollama", "local", "llamacpp", "lmstudio", "vllm", "tgi"}


def _normalize_model_id(model: str) -> str:
    """Strip Bedrock-style region/vendor prefixes + revision suffix.

    Bedrock IDs look like `us.anthropic.claude-opus-4-1-20250805-v1:0`. The
    context table is keyed on the bare model family (`claude-opus-4-1`), so
    we drop the leading `<region>.<vendor>.` segments and the trailing
    `-v<N>[:N]` revision suffix. Also strips Anthropic-style date suffixes
    like `-20251001` so `claude-haiku-4-5-20251001` resolves to
    `claude-haiku-4-5`.
    """
    key = (model or "").lower()
    # Drop trailing `:0` provisioned-throughput suffix.
    key = key.split(":", 1)[0]
    # Drop `<region>.<vendor>.` or `<vendor>.` prefix.
    parts = key.split(".")
    if len(parts) >= 2 and parts[-2] in {
        "anthropic", "amazon", "meta", "mistral", "cohere", "ai21",
    }:
        key = parts[-1]
    # Drop trailing `-vN` revision suffix.
    key = re.sub(r"-v\d+$", "", key)
    # Drop trailing `-YYYYMMDD` date suffix.
    key = re.sub(r"-\d{8}$", "", key)
    return key


def model_context_limits(provider: str, model: str) -> tuple[int, int]:
    """Return (max_context_tokens, max_output_tokens) for the model.

    - Exact match on the normalized key wins.
    - Otherwise longest-prefix match wins (so `claude-opus-4-7` beats
      `claude-opus-4`).
    - Local providers fall back to `_LOCAL_DEFAULT`.
    - Unknown cloud models return `(0, 0)` so callers can decide how to
      render "limit unknown" cases.
    """
    if (provider or "").lower() in _LOCAL_PROVIDERS:
        return _LOCAL_DEFAULT
    if not model:
        return (0, 0)
    key = _normalize_model_id(model)
    if key in _CONTEXT:
        return _CONTEXT[key]
    for k in sorted(_CONTEXT, key=len, reverse=True):
        if key.startswith(k):
            return _CONTEXT[k]
    return (0, 0)
