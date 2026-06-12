"""
vibe-cli — Subagent registry.

Subagents are specialised LangGraph agents the main agent can spawn via the
`spawn_subagent` tool. Each type ships its own system prompt + tool whitelist;
the model used by each type is configurable per-user via `/subagents` (saved to
preferences.json) and falls back to the active chat model when no override
exists.

This module exposes:
  - SUBAGENT_REGISTRY: name -> Subagent class
  - register(cls): decorator/function for adding a new type
  - get(name): look up a class by name (None if missing)
  - list_types(): registered names, sorted

Built-in types are imported eagerly so the registry is populated at import time.
"""

from __future__ import annotations

from .base import Subagent, SubagentResult, run_subagent

SUBAGENT_REGISTRY: dict[str, type[Subagent]] = {}


def register(cls: type[Subagent]) -> type[Subagent]:
    name = getattr(cls, "name", None)
    if not name:
        raise ValueError(f"Subagent class {cls!r} is missing a 'name' attribute")
    SUBAGENT_REGISTRY[name] = cls
    return cls


def get(name: str) -> type[Subagent] | None:
    return SUBAGENT_REGISTRY.get(name)


def list_types() -> list[str]:
    return sorted(SUBAGENT_REGISTRY.keys())


# Eagerly import built-ins so they self-register.
from .types import general as _general  # noqa: E402, F401
from .types import code_search as _code_search  # noqa: E402, F401
from .types import web_research as _web_research  # noqa: E402, F401


__all__ = [
    "Subagent",
    "SubagentResult",
    "SUBAGENT_REGISTRY",
    "register",
    "get",
    "list_types",
    "run_subagent",
]