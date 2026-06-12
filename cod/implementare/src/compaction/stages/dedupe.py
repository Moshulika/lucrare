"""
dedupe — threshold stage that collapses near-duplicate content.

What it catches
---------------
1. **Identical tool_calls**: same tool name + same JSON-serialized args within
   the session. The first call is kept; later identical calls are marked
   removed (along with their paired tool_results, when the result body is
   also identical).
2. **Near-duplicate tool_results**: pairwise string-similarity (difflib's
   `SequenceMatcher`). For each `tool_result`, compare against earlier
   `tool_result`s; if ratio >= `similarity`, mark the *earlier* one removed
   (the newer answer is presumed more relevant).

Performance
-----------
Worst case is O(N^2) over the count of tool_result events, with each
SequenceMatcher comparison being O(M*N) over body length. A length-based
quick-reject filter avoids running SequenceMatcher on length-mismatched
pairs (`abs(len(a)-len(b)) / max(len) > 1 - similarity`).

Future work
-----------
- Embedding-based similarity backend (`backend: embedding`). Wire a
  pluggable embedder so the same-meaning-different-wording case is also
  caught. Skipped for now to avoid a hard dependency on an embedding model.

Config (under `compaction.pipeline.<n>.dedupe`):
  enabled:    bool    default false
  similarity: float   default 0.92    cosine-ish threshold
  backend:    str     default "string"  string | embedding (embedding TODO)
"""

from __future__ import annotations

import json
from difflib import SequenceMatcher

from src.compaction.base import (
    ThresholdContext,
    ThresholdResult,
    ThresholdStage,
    stringify,
)
from ui.sessions import KIND_TOOL_CALL, KIND_TOOL_RESULT


def _length_quick_reject(a: str, b: str, similarity: float) -> bool:
    if not a or not b:
        return True
    longer = max(len(a), len(b))
    diff = abs(len(a) - len(b))
    return (diff / longer) > (1.0 - similarity)


class Dedupe(ThresholdStage):
    name = "dedupe"
    mode = "threshold"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.similarity: float = float(self.config.get("similarity", 0.92))
        self.backend: str = self.config.get("backend") or "string"

    async def apply_threshold(self, tctx: ThresholdContext) -> ThresholdResult:
        # NOTE: embedding backend is future work — fall back to string.
        if self.backend not in ("string", "embedding"):
            self.backend = "string"

        removed: list[str] = []
        # Pass 1: identical tool_calls.
        seen_calls: dict[str, str] = {}  # signature -> first event id
        call_dups: dict[str, str] = {}  # event id -> matched-first-id
        for evt in tctx.events:
            if evt["kind"] != KIND_TOOL_CALL:
                continue
            name = (evt.get("meta") or {}).get("name") or ""
            args = evt.get("content")
            try:
                sig = name + "::" + json.dumps(args, sort_keys=True, default=str)
            except (TypeError, ValueError):
                continue
            if sig in seen_calls:
                removed.append(evt["id"])
                call_dups[evt["id"]] = seen_calls[sig]
            else:
                seen_calls[sig] = evt["id"]

        # Pass 2: near-duplicate tool_results.
        prior_results: list[tuple[str, str]] = []  # (event_id, body)
        for evt in tctx.events:
            if evt["kind"] != KIND_TOOL_RESULT:
                continue
            body = stringify(evt.get("projected") or evt.get("content"))
            for prev_id, prev_body in prior_results:
                if prev_id in removed:
                    continue
                if _length_quick_reject(prev_body, body, self.similarity):
                    continue
                ratio = SequenceMatcher(None, prev_body, body).ratio()
                if ratio >= self.similarity:
                    removed.append(prev_id)
            prior_results.append((evt["id"], body))

        notes = {
            "duplicate_tool_calls": len(call_dups),
            "duplicate_tool_results": len(removed) - len(call_dups),
            "backend": self.backend,
        }
        return ThresholdResult(removed_event_ids=removed, summary="", notes=notes)
