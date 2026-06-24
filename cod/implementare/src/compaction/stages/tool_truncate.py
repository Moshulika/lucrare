from __future__ import annotations
from src.compaction.base import Stage
from src.compaction.stages._tool_helpers import _truncate_to_excerpt, event_body
from ui.sessions import KIND_TOOL_RESULT

class ToolTruncate(Stage):
    name = "tool_truncate"
    mode = "eager"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.max_tool_tokens: int = int(self.config.get("max_tool_tokens", 2000))
        head_tail = self.config.get("head_tail") or [800, 400]
        self.head_tokens: int = int(head_tail[0])
        self.tail_tokens: int = int(head_tail[1])

    def apply_eager(self, event, *, store) -> bool:
        if event.get("kind") != KIND_TOOL_RESULT:
            return False
        body = event_body(event)
        result = _truncate_to_excerpt(
            body,
            store=store,
            max_tool_tokens=self.max_tool_tokens,
            head_tokens=self.head_tokens,
            tail_tokens=self.tail_tokens,
            marker_label="tool_truncate",
        )
        if result is None:
            return False

        event["projected"] = result.projected
        if event.get("meta") is None:
            event["meta"] = {}
        comp = event["meta"].setdefault("compaction", {})
        comp["tool_truncate"] = {
            "original_bytes": result.original_bytes,
            "artifact": result.artifact_handle,
        }
        return True
