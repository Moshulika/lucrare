"""
format_compress — eager stage that re-serialises verbose JSON tool I/O denser.

Targets `KIND_TOOL_CALL` (args dict) and `KIND_TOOL_RESULT` (string body that
parses as JSON). Strips null / empty-collection fields and re-serialises
without indentation.

Config (under `compaction.pipeline.<n>.format_compress`):
  enabled:   bool                       default false
  per_tool:  { tool_name: { mode } }    optional per-tool overrides
             mode: "json_min" (default) — strip empty + minify JSON
                   "passthrough"        — leave the event alone
"""

from __future__ import annotations

import json
from typing import Any

from src.compaction.base import Stage, stringify
from ui.sessions import KIND_TOOL_CALL, KIND_TOOL_RESULT


def _strip_empty(obj: Any) -> Any:
    """Recursively drop None / empty-string / empty-list / empty-dict values."""
    if isinstance(obj, dict):
        return {
            k: _strip_empty(v)
            for k, v in obj.items()
            if v is not None and v != "" and v != [] and v != {}
        }
    if isinstance(obj, list):
        return [_strip_empty(x) for x in obj if x is not None and x != "" and x != [] and x != {}]
    return obj


class FormatCompress(Stage):
    name = "format_compress"
    mode = "eager"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.per_tool: dict[str, dict] = self.config.get("per_tool") or {}

    def _mode_for_tool(self, tool_name: str) -> str:
        return (self.per_tool.get(tool_name) or {}).get("mode", "json_min")

    def apply_eager(self, event, *, store) -> bool:
        kind = event.get("kind")
        if kind not in (KIND_TOOL_CALL, KIND_TOOL_RESULT):
            return False
        tool_name = (event.get("meta") or {}).get("name") or ""
        if self._mode_for_tool(tool_name) == "passthrough":
            return False

        original = event.get("content")
        if kind == KIND_TOOL_CALL:
            if not isinstance(original, (dict, list)):
                return False
            compact = _strip_empty(original)
            new_text = json.dumps(compact, separators=(",", ":"), ensure_ascii=False)
        else:
            text = stringify(original)
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                return False
            compact = _strip_empty(parsed)
            new_text = json.dumps(compact, separators=(",", ":"), ensure_ascii=False)

        # No-op if compaction didn't actually save anything.
        old_text = stringify(original)
        if len(new_text) >= len(old_text):
            return False

        event["projected"] = new_text
        if event.get("meta") is None:
            event["meta"] = {}
        comp = event["meta"].setdefault("compaction", {})
        comp["format_compress"] = {
            "original_chars": len(old_text),
            "compact_chars": len(new_text),
        }
        return True
