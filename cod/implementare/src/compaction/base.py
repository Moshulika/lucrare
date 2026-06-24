from __future__ import annotations
import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable
from src.artifacts import ArtifactStore

class CompactionConfigError(ValueError):
    ...

class Stage(ABC):
    ...

    name: str = "stage"
    mode: str = "eager"

    def __init__(self, config: dict | None = None) -> None:
        self.config: dict = config or {}
        self.enabled: bool = bool(self.config.get("enabled", False))

    @abstractmethod
    def apply_eager(self, event: dict, *, store: ArtifactStore) -> bool:
        ...

    async def apply_eager_async(self, ectx: "EagerContext") -> bool:
        return self.apply_eager(ectx.event, store=ectx.store)

@dataclass
class EagerContext:

    event: dict
    store: ArtifactStore
    session_id: str | None = None
    llm: Any | None = None
    background_tasks: list[asyncio.Task] = field(default_factory=list)
    on_event_updated: Callable[[dict], None] | None = None

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.background_tasks.append(task)
        return task

def stringify(content: Any) -> str:
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
    if not text:
        return 0
    return max(1, len(text) // 4)

@dataclass
class ThresholdContext:

    events: list[dict]
    current_size: int
    target_size: int
    min_turns_kept: int
    store: ArtifactStore
    llm: Any | None = None 


@dataclass
class ThresholdResult:
    removed_event_ids: list[str] = field(default_factory=list)
    summary: str = ""
    freed_estimate: int = 0
    notes: dict = field(default_factory=dict)


class ThresholdStage(ABC):
    name: str = "threshold_stage"
    mode: str = "threshold"

    def __init__(self, config: dict | None = None) -> None:
        self.config: dict = config or {}
        self.enabled: bool = bool(self.config.get("enabled", False))

    @abstractmethod
    async def apply_threshold(self, tctx: ThresholdContext) -> ThresholdResult:
        """Decide what to remove (and optionally summarise)."""
