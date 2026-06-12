"""
tool_truncate — eager stage that caps individual tool_result events.

When a tool result lands and its size exceeds `max_tool_tokens`, this stage:
  1. Stashes the full payload in the session's artifact store.
  2. Builds a head + tail excerpt with a middle marker pointing at the
     `@artifact:<sha>` handle.
  3. Writes the result to `event['projected']`. The original `content`
     remains untouched on disk — the file stays a faithful log.

The agent can re-hydrate the full payload via the `fetch_artifact` tool.

Mutex
-----
`tool_truncate` is mutually exclusive with `tool_summarize` and
`ref_substitute` (they all rewrite the same event slot). The mutex is
enforced at startup by `build_eager_pipeline` — see
`src/compaction/__init__.py:EAGER_EXCLUSION_GROUPS`.

Config (under `compaction.pipeline.<n>.tool_truncate`):
  enabled:           bool   default false
  max_tool_tokens:   int    default 2000   — threshold above which to truncate
  head_tail:         [int, int] default [800, 400]   — head / tail token budget
"""

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
