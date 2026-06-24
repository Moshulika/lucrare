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
    provider: str
    model: str

    def supports(self, kind: str) -> bool:
        if kind == "text":
            return True
        return (
            preferences.get_model_capability(self.provider, self.model, kind) != "no"
        )

    def recorded(self, kind: str) -> str | None:
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
    if kind not in ("image", "pdf"):
        return
    current = preferences.get_model_capability(provider, model, kind)
    if current is None:
        preferences.set_model_capability(provider, model, kind, "yes")

def clear(provider: str, model: str | None = None) -> bool:
    return preferences.clear_model_capabilities(provider, model)

def peers_supporting(
    provider: str, kind: str, provider_configs: dict
) -> list[str]:
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
    "Capabilities","Kind","KNOWN_KINDS",
    "clear","get_capabilities","peers_supporting",
    "record_supported","record_unsupported",
]
