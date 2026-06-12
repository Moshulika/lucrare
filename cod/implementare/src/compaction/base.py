"""
vibe-cli — Compaction stage base class and shared helpers.
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from src.artifacts import ArtifactStore


class CompactionConfigError(ValueError):
    """Raised at startup when the configured compaction pipeline is invalid.

    Today the only validation is the eager-stage exclusion-group check
    (`tool_truncate` / `tool_summarize` / `ref_substitute` are mutually
    exclusive — pick one or none) but more rules can be added without
    changing call sites: `ui/app.py` catches this once at startup and
    aborts with a red banner pointing at `config.yml`.
    """


class Stage(ABC):
    """Base class for a compaction stage.

    Subclasses set `name` and `mode` ("eager" or "threshold") and implement
    `apply_eager` (sync, the historical contract) and/or `apply_eager_async`
    (async, opt-in for stages that need an LLM or want to spawn background
    work). Every stage reads its own config dict and exposes `self.enabled`.

    Stages that don't need async — `tool_truncate`, `format_compress`,
    `ref_substitute` — only override `apply_eager`. `apply_eager_async`'s
    default implementation just delegates so the orchestrator can treat
    every stage uniformly without forcing them all to become coroutines.
    """

    name: str = "stage"
    mode: str = "eager"

    def __init__(self, config: dict | None = None) -> None:
        self.config: dict = config or {}
        self.enabled: bool = bool(self.config.get("enabled", False))

    @abstractmethod
    def apply_eager(self, event: dict, *, store: ArtifactStore) -> bool:
        """Mutate `event['projected']` in place if the stage applies.

        Args:
            event: The session event dict that was just appended.
            store: A per-session artifact store, used by stages that need
                to spill payloads out of the message stream.

        Returns:
            True if the stage modified the event, False otherwise.
        """

    async def apply_eager_async(self, ectx: "EagerContext") -> bool:
        """Async entry point for eager stages. Default: delegate to sync.

        Stages that need an LLM (`tool_summarize`) override this and use
        `ectx.llm` / `ectx.spawn(...)` to schedule background work. The
        return value matches `apply_eager`: True iff the event was
        modified during the synchronous portion (background tasks may
        further modify it later via `ectx.on_event_updated`).
        """
        return self.apply_eager(ectx.event, store=ectx.store)


# ---------------------------------------------------------
# Eager-pipeline context
# ---------------------------------------------------------
@dataclass
class EagerContext:
    """Inputs handed to a stage when the eager pipeline runs.

    Eager stages used to be purely synchronous and content-only — they got
    an event and an artifact store. `tool_summarize` changes that: it needs
    to call out to an LLM and patch the event later, without blocking the
    tool loop. `EagerContext` packs everything a stage may need:

    - `event` / `store` — historical inputs (still consumed by the sync
      `apply_eager` path).
    - `session_id` — for log correlation.
    - `llm` — optional, resolved by the caller via the same path used by
      threshold stages (see `ui/app.py:_resolve_compaction_llm`).
    - `background_tasks` — accumulator the caller can drain at turn
      boundary so async upgrades don't leak across turns.
    - `on_event_updated` — callback the stage invokes after a background
      task patches the event so the session is re-saved to disk.
    """

    event: dict
    store: ArtifactStore
    session_id: str | None = None
    llm: Any | None = None
    background_tasks: list[asyncio.Task] = field(default_factory=list)
    on_event_updated: Callable[[dict], None] | None = None

    def spawn(self, coro) -> asyncio.Task:
        """Schedule a background task and track it for later draining.

        Returned task is also appended to `background_tasks` so the caller
        can `asyncio.gather(*tasks)` at turn boundary.
        """
        task = asyncio.create_task(coro)
        self.background_tasks.append(task)
        return task


# ---------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------
def stringify(content: Any) -> str:
    """Loose stringify that handles LangChain-style content lists / dicts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            if isinstance(block, dict):
                out.append(block.get("text") or block.get("input") or "")
            else:
                out.append(str(block))
        return "".join(out)
    if isinstance(content, dict):
        try:
            return json.dumps(content)
        except (TypeError, ValueError):
            return str(content)
    return str(content)


def approx_tokens(text: str) -> int:
    """~4 chars-per-token estimate. Conservative; matches the session metrics."""
    if not text:
        return 0
    return max(1, len(text) // 4)


# ---------------------------------------------------------
# Threshold stage scaffolding
# ---------------------------------------------------------
@dataclass
class ThresholdContext:
    """Inputs handed to a threshold-mode stage when it runs.

    The stage reads from `events` (the full session log, in order) and is
    free to consult `current_size` / `target_size` / `min_turns_kept` to
    decide how aggressively to act. `llm` is optional — only the bulk
    summarize / hybrid strategies need it.
    """

    events: list[dict]
    current_size: int
    target_size: int
    min_turns_kept: int
    store: ArtifactStore
    llm: Any | None = None  # LangChain BaseChatModel; absent if not configured


@dataclass
class ThresholdResult:
    """A stage's verdict.

    A threshold stage *declares* which events should be considered removed
    and (optionally) supplies a summary. The orchestrator aggregates these
    across stages and writes a single KIND_COMPACTION event; the live
    message list is then re-derived from the session via to_messages().
    """

    removed_event_ids: list[str] = field(default_factory=list)
    summary: str = ""
    freed_estimate: int = 0
    notes: dict = field(default_factory=dict)


class ThresholdStage(ABC):
    """Base class for stages that run only when context utilisation crosses
    the configured threshold. They mutate whole turns at once.

    Threshold stages are async because some (bulk_summarize / bulk_hybrid)
    issue an LLM call.
    """

    name: str = "threshold_stage"
    mode: str = "threshold"

    def __init__(self, config: dict | None = None) -> None:
        self.config: dict = config or {}
        self.enabled: bool = bool(self.config.get("enabled", False))

    @abstractmethod
    async def apply_threshold(self, tctx: ThresholdContext) -> ThresholdResult:
        """Decide what to remove (and optionally summarise)."""
