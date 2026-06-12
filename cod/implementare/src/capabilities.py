"""
vibe-cli — Multimodal capability detection.

We assume every model can accept image / PDF / text attachments until a
provider tells us otherwise. The first time a provider returns an
attachment-related error (classified by `_classify_attachment_error` in
`src/main.py`), we record `"no"` for the offending `(provider, model, kind)`
in `~/.vibe-cli/preferences.json`. From then on, attaches against that
combination are refused locally before the LLM call.

This module reads / writes that learned state via `ui.preferences`. There is
no `config.yml` schema for capability — provider APIs don't expose it
reliably enough across vendors (only Bedrock's `list_foundation_models`
returns `inputModalities`; OpenAI / Anthropic / Gemini all omit it).

Public API
----------
- `Capabilities(provider, model)` — read-only view; `supports(kind)` returns
  False only when we have an explicit recorded "no".
- `get_capabilities(provider, model)` — convenience constructor.
- `record_unsupported(provider, model, kind, reason)` — store a learned "no".
- `record_supported(provider, model, kind)` — store a confirmed "yes"
  (diagnostic; not required for gating).
- `peers_supporting(provider, kind, catalog)` — list peer models in the same
  provider that are not known-bad for the kind (used to make rejection
  messages helpful).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from ui import preferences

logger = logging.getLogger(__name__)

Kind = Literal["image", "pdf", "text"]
KNOWN_KINDS: tuple[Kind, ...] = ("image", "pdf", "text")


@dataclass(frozen=True)
class Capabilities:
    """Capability view for a single (provider, model) pair."""

    provider: str
    model: str

    def supports(self, kind: str) -> bool:
        """True unless we have an explicit recorded "no". Text is always supported."""
        if kind == "text":
            return True
        return (
            preferences.get_model_capability(self.provider, self.model, kind) != "no"
        )

    def recorded(self, kind: str) -> str | None:
        """Return the literal recorded value ("yes" / "no" / None for unset)."""
        return preferences.get_model_capability(self.provider, self.model, kind)


def get_capabilities(provider: str, model: str) -> Capabilities:
    return Capabilities(provider=provider, model=model)


def record_unsupported(
    provider: str,
    model: str,
    kind: str,
    *,
    reason: str = "",
) -> None:
    """Mark (provider, model, kind) as not supported and log the reason."""
    if kind not in ("image", "pdf"):
        return
    preferences.set_model_capability(provider, model, kind, "no")
    logger.warning(
        "model_capabilities.learned: provider=%s model=%s kind=%s value=no reason=%s",
        provider,
        model,
        kind,
        reason[:200],
    )


def record_supported(provider: str, model: str, kind: str) -> None:
    """Mark (provider, model, kind) as confirmed supported (diagnostic)."""
    if kind not in ("image", "pdf"):
        return
    # Only flip an unrecorded entry → "yes". Don't overwrite a recorded "no".
    current = preferences.get_model_capability(provider, model, kind)
    if current is None:
        preferences.set_model_capability(provider, model, kind, "yes")


def clear(provider: str, model: str | None = None) -> bool:
    """Forget recorded capabilities for a provider (or specific model). Returns True
    if any state was removed."""
    return preferences.clear_model_capabilities(provider, model)


def peers_supporting(
    provider: str, kind: str, provider_configs: dict
) -> list[str]:
    """Return models in the same provider catalog that are *not* known to reject
    `kind`. Models with no recorded state are included (optimistic default)."""
    catalog = provider_configs.get(provider, {}).get("models") or []
    out: list[str] = []
    for entry in catalog:
        name = entry if isinstance(entry, str) else entry.get("name")
        if not name:
            continue
        if preferences.get_model_capability(provider, name, kind) == "no":
            continue
        out.append(name)
    return out


__all__ = [
    "Capabilities",
    "Kind",
    "KNOWN_KINDS",
    "clear",
    "get_capabilities",
    "peers_supporting",
    "record_supported",
    "record_unsupported",
]
