"""
importance_prune — threshold stage that drops low-value events by role.

Rules
-----
- Events whose `kind` is in `drop_kinds` are unconditionally removed
  (default: `assistant_thinking`).
- The pseudo-kind `tool_result_last` in `keep_kinds` means: within each turn,
  keep only the *last* tool_result; mark earlier tool_results in that turn as
  removed.
- The most recent `min_turns_kept` turns are protected — no events from
  those turns are pruned, even if they would normally be dropped. This is
  the same guardrail used by the bulk strategies.
- `human` and `system_prompt` events are never pruned (they anchor the
  conversation).

Config (under `compaction.pipeline.<n>.importance_prune`):
  enabled:    bool             default false
  keep_kinds: list[str]        default ["human","system_prompt",
                                        "assistant_text","tool_result_last"]
  drop_kinds: list[str]        default ["assistant_thinking"]
"""

from __future__ import annotations

from src.compaction.base import (
    ThresholdContext,
    ThresholdResult,
    ThresholdStage,
)
from ui.sessions import (
    KIND_HUMAN,
    KIND_SYSTEM_PROMPT,
    KIND_TOOL_RESULT,
)

ALWAYS_KEEP_KINDS = (KIND_HUMAN, KIND_SYSTEM_PROMPT)


class ImportancePrune(ThresholdStage):
    name = "importance_prune"
    mode = "threshold"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.keep_kinds: list[str] = list(
            self.config.get("keep_kinds")
            or ["human", "system_prompt", "assistant_text", "tool_result_last"]
        )
        self.drop_kinds: list[str] = list(
            self.config.get("drop_kinds") or ["assistant_thinking"]
        )

    async def apply_threshold(self, tctx: ThresholdContext) -> ThresholdResult:
        # Identify the protected turn-ids: the last `min_turns_kept` turns
        # by chronological order of their first human event.
        turn_order: list[str] = []
        for evt in tctx.events:
            tid = evt.get("turn_id")
            if tid and tid not in turn_order:
                turn_order.append(tid)
        protected_turns = set(turn_order[-tctx.min_turns_kept :]) if tctx.min_turns_kept > 0 else set()

        # Group tool_result event ids by turn.
        tool_results_by_turn: dict[str, list[str]] = {}
        for evt in tctx.events:
            if evt["kind"] == KIND_TOOL_RESULT:
                tid = evt.get("turn_id") or ""
                tool_results_by_turn.setdefault(tid, []).append(evt["id"])

        # Decide which event ids to drop.
        keep_last_tool_result = "tool_result_last" in self.keep_kinds
        last_tool_result_id_per_turn: dict[str, str] = {
            tid: ids[-1] for tid, ids in tool_results_by_turn.items() if ids
        }

        removed: list[str] = []
        for evt in tctx.events:
            kind = evt["kind"]
            tid = evt.get("turn_id")
            if kind in ALWAYS_KEEP_KINDS:
                continue
            if tid in protected_turns:
                continue
            if kind in self.drop_kinds:
                removed.append(evt["id"])
                continue
            if kind == KIND_TOOL_RESULT and keep_last_tool_result:
                if last_tool_result_id_per_turn.get(tid) != evt["id"]:
                    removed.append(evt["id"])

        notes = {
            "protected_turns": len(protected_turns),
            "drop_kinds": self.drop_kinds,
            "keep_last_tool_result": keep_last_tool_result,
        }
        return ThresholdResult(removed_event_ids=removed, summary="", notes=notes)
