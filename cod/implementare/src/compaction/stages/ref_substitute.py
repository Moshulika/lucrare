"""
ref_substitute — eager stage that replaces a large tool_result with a tiny
reference + excerpt.

Stronger than `tool_truncate`: instead of preserving head + tail, the
projected body is essentially just the `@artifact:<sha>` handle and a
short excerpt. The agent must call `fetch_artifact` to recover the body.

Because this *removes information from the LLM's working set*, leave it
disabled until you have the `fetch_artifact` tool wired and the system
prompt / agent contract updated to teach the model when to re-fetch.

Config (under `compaction.pipeline.<n>.ref_substitute`):
  enabled:          bool   default false
  min_ref_tokens:   int    default 1500   — only substitute when bigger
  excerpt_tokens:   int    default 200    — head excerpt kept inline
"""

from __future__ import annotations

from src.compaction.base import Stage, approx_tokens, stringify
from ui.sessions import KIND_TOOL_RESULT


class RefSubstitute(Stage):
    name = "ref_substitute"
    mode = "eager"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.min_ref_tokens: int = int(self.config.get("min_ref_tokens", 1500))
        self.excerpt_tokens: int = int(self.config.get("excerpt_tokens", 200))

    def apply_eager(self, event, *, store) -> bool:
        if event.get("kind") != KIND_TOOL_RESULT:
            return False
        # Skip if a previous stage already replaced the body with a reference.
        existing_meta = (event.get("meta") or {}).get("compaction") or {}
        if "ref_substitute" in existing_meta:
            return False

        # Evaluate against the *original* content size: if the underlying
        # tool result was big, substitute regardless of whether an earlier
        # stage already truncated the projected view. The "already
        # substituted" guard above is what prevents double-fires.
        original_body = stringify(event.get("content"))
        if approx_tokens(original_body) < self.min_ref_tokens:
            return False
        ref = store.put(original_body, kind="tool_result", ext="txt")
        excerpt_chars = self.excerpt_tokens * 4
        excerpt = original_body[:excerpt_chars]
        projected = (
            f"[ref_substitute] {ref.handle} ({ref.bytes_} bytes) — "
            f"call fetch_artifact to retrieve.\n--- excerpt ---\n{excerpt}"
        )

        event["projected"] = projected
        if event.get("meta") is None:
            event["meta"] = {}
        comp = event["meta"].setdefault("compaction", {})
        comp["ref_substitute"] = {
            "original_bytes": ref.bytes_,
            "artifact": ref.handle,
        }
        return True
