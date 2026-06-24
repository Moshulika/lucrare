from __future__ import annotations
from typing import Any
from langchain_core.messages import HumanMessage, SystemMessage
from src.compaction.base import (
    ThresholdContext,
    ThresholdResult,
    ThresholdStage,
    approx_tokens,
    stringify,
)
from ui.sessions import (
    KIND_ASSISTANT_TEXT,
    KIND_HUMAN,
    KIND_SYSTEM_PROMPT,
)

class Bulk(ThresholdStage):
    name = "bulk"
    mode = "threshold"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        strategy = (self.config.get("strategy") or "hybrid").lower()
        if strategy not in ("trim", "summarize", "hybrid"):
            strategy = "hybrid"
        self.strategy: str = strategy
        sumcfg = self.config.get("summarize") or {}
        self.max_summary_tokens: int = int(sumcfg.get("max_summary_tokens", 1500))

    async def apply_threshold(self, tctx: ThresholdContext) -> ThresholdResult:
        effective = self.strategy
        fallback_reason: str | None = None
        if effective in ("summarize", "hybrid") and tctx.llm is None:
            fallback_reason = f"{effective} requested but no LLM available - using trim"
            effective = "trim"

        turn_order: list[str] = []
        events_by_turn: dict[str, list[dict]] = {}
        for evt in tctx.events:
            tid = evt.get("turn_id")
            if not tid:
                continue
            if tid not in events_by_turn:
                events_by_turn[tid] = []
                turn_order.append(tid)
            events_by_turn[tid].append(evt)

        if not turn_order:
            return ThresholdResult(notes={"reason": "no turns yet"})
        protected = set(turn_order[-tctx.min_turns_kept :]) if tctx.min_turns_kept > 0 else set()
        droppable = [t for t in turn_order if t not in protected]

        def _turn_size(tid: str) -> int:
            return sum(
                approx_tokens(stringify(e.get("projected") or e.get("content")))
                for e in events_by_turn[tid]
            )

        size_remaining = tctx.current_size
        drop: list[str] = []
        for tid in droppable:
            if size_remaining <= tctx.target_size:
                break
            drop.append(tid)
            size_remaining -= _turn_size(tid)

        if not drop:
            notes: dict[str, Any] = {"reason": "already under target", "strategy": effective}
            if fallback_reason:
                notes["fallback"] = fallback_reason
            return ThresholdResult(notes=notes)

        removed_ids = [
            evt["id"]
            for tid in drop
            for evt in events_by_turn[tid]
            if evt["kind"] != KIND_SYSTEM_PROMPT
        ]

        summary = ""
        if effective in ("summarize", "hybrid"):
            summary = await self._summarize_turns(
                tctx.llm, [events_by_turn[t] for t in drop]
            )

        notes = {
            "strategy": effective,
            "dropped_turns": len(drop),
            "protected_turns": len(protected),
        }
        if fallback_reason:
            notes["fallback"] = fallback_reason

        return ThresholdResult(
            removed_event_ids=removed_ids,
            summary=summary,
            notes=notes,
        )

    async def _summarize_turns(self, llm, turn_event_lists: list[list[dict]]) -> str:
        """Ask the LLM to compress dropped turns into a short summary."""
        lines: list[str] = []
        for evts in turn_event_lists:
            human = next((e for e in evts if e["kind"] == KIND_HUMAN), None)
            if human is not None:
                lines.append(
                    f"User: {stringify(human.get('projected') or human.get('content'))}"
                )
            final_text = ""
            for e in evts:
                if e["kind"] == KIND_ASSISTANT_TEXT:
                    text = stringify(e.get("projected") or e.get("content"))
                    if text.strip():
                        final_text = text
            if final_text:
                lines.append(f"Assistant: {final_text}")

        if not lines:
            return ""

        transcript = "\n".join(lines)
        instr = (
            "Summarize the following conversation excerpt for a future-self of "
            "the assistant who must continue the conversation. Preserve user "
            "intent, key decisions, names, file paths, and any open questions. "
            f"Aim for under {self.max_summary_tokens} tokens.\n\n"
            "--- transcript ---\n"
            f"{transcript}\n"
            "--- end transcript ---"
        )
        prompt = [
            SystemMessage(content="You compress conversations losslessly for the parts that matter."),
            HumanMessage(content=instr),
        ]
        try:
            response = await llm.ainvoke(prompt)
        except Exception as exc:  # noqa: BLE001
            return f"(summarization failed: {exc})"
        text = response.content
        if isinstance(text, list):
            text = "".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in text
            )
        return str(text or "").strip()
