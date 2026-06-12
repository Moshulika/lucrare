"""
tool_summarize — eager stage that LLM-summarizes individual oversized tool
results in place.

Why a separate stage from `tool_truncate`?
------------------------------------------
`tool_truncate` keeps a head + tail excerpt with a marker pointing at the
artifact handle. That's correct context but it discards the middle, which
is often where the interesting bit lives (think: a large `read_file`
result where the relevant function is on line 200 of 800). `tool_summarize`
asks an LLM to produce a short, semantic summary of the *whole* tool
output, optionally pinned to the tool name (so a `web_search` summary
reads differently from a `run_command` summary).

Mutex with `tool_truncate`
--------------------------
The two stages are mutually exclusive — see
`src/compaction/__init__.py:EAGER_EXCLUSION_GROUPS`. Enabling both would
have the second one clobber the first's `projected` field, which is
always a config mistake. Pick one. (`tool_summarize` is strictly stronger
in the happy path because it falls back to truncate on any failure.)

Truncate-as-fallback
--------------------
Every invocation runs in two phases:

1. **Synchronous truncate.** We immediately produce a head + tail excerpt
   identical to `tool_truncate`'s output and write it to `event['projected']`.
   The event is now safe to ship to the next LLM call: even if step 2
   never completes, the context budget is bounded.
2. **Async LLM upgrade (background task).** We spawn an `asyncio.Task`
   that calls the configured compaction model (see
   `ui/preferences.py:get_compaction_model` — defaults to the active
   chat model). On success we replace `event['projected']` with the
   summary and stamp `meta.compaction.tool_summarize.state = "summarized"`.
   On any failure (LLM unreachable, timeout, exception) the truncated
   form stays in place, state flips to `truncated_fallback`, and we log
   `compaction.eager.summarize_fallback` so operators can tell why their
   summaries aren't landing.

Lifecycle of the background task
--------------------------------
The task is appended to `ectx.background_tasks` and surfaces in
`ui/app.py`'s `ctx['pending_summary_tasks']`. The turn loop drains it at
the top of the next iteration with a 2s timeout. If it finishes:
- in time → next prompt sees the summary
- after the budget → it still patches the event when it does finish
  (via `ectx.on_event_updated`); the upgrade just lands one turn later

The truncated fallback is on disk from step 1, so the user is never
blocked and we never lose progress.

Config (under `compaction.pipeline.<n>.tool_summarize`):
  enabled:                bool        default false
  max_tool_tokens:        int         default 2000   — gate, same as tool_truncate
  head_tail:              [int, int]  default [800, 400]   — fallback excerpt
  timeout_s:              float       default 30     — LLM call hard cap
  max_summary_tokens:     int         default 500    — target summary length
  model:                  str | null  default null   — informational; the live
                                                       resolver always uses
                                                       preferences.compaction_model
                                                       falling back to the chat
                                                       model. This key is kept
                                                       for parity with bulk's
                                                       summarize.model and may
                                                       be promoted later.
"""

from __future__ import annotations

import asyncio
import logging
import time

from langchain_core.messages import HumanMessage, SystemMessage

from src.compaction.base import EagerContext, Stage, approx_tokens
from src.compaction.stages._tool_helpers import _truncate_to_excerpt, event_body
from src.logging_setup import emit_event, get_logger
from ui.sessions import KIND_TOOL_RESULT

_log = get_logger("compaction.tool_summarize")


_PROMPT_SYSTEM = (
    "You are a compaction assistant. Your job is to compress tool output "
    "into a short, faithful summary so a future LLM turn can act on it "
    "without re-reading the full payload. Preserve concrete facts: file "
    "paths, identifiers, error messages, numbers, ordering. Drop "
    "boilerplate. Never invent details that aren't in the input."
)


def _build_prompt(
    *,
    tool_name: str,
    body: str,
    max_summary_tokens: int,
) -> list:
    """Build the (System, Human) prompt for summarising a single tool result."""
    instr = (
        f"The following is the full output of the tool `{tool_name or 'unknown'}`. "
        f"Summarize it in under ~{max_summary_tokens} tokens. If the output is "
        f"primarily error text, surface the error verbatim. If it lists items "
        f"(files, results), keep their identifiers.\n\n"
        f"--- tool output ---\n{body}\n--- end tool output ---"
    )
    return [
        SystemMessage(content=_PROMPT_SYSTEM),
        HumanMessage(content=instr),
    ]


def _extract_text(response) -> str:
    """Pull plain text out of a LangChain AIMessage response, content-list-safe."""
    text = getattr(response, "content", "")
    if isinstance(text, list):
        text = "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in text
        )
    return str(text or "").strip()


def _extract_usage(response) -> dict:
    """Mirror of `ui/app.py:_extract_usage` — pull token counts off the AIMessage."""
    um = getattr(response, "usage_metadata", None) or {}
    return {
        "input": int(um.get("input_tokens") or 0),
        "output": int(um.get("output_tokens") or 0),
    }


class ToolSummarize(Stage):
    name = "tool_summarize"
    mode = "eager"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.max_tool_tokens: int = int(self.config.get("max_tool_tokens", 2000))
        head_tail = self.config.get("head_tail") or [800, 400]
        self.head_tokens: int = int(head_tail[0])
        self.tail_tokens: int = int(head_tail[1])
        self.timeout_s: float = float(self.config.get("timeout_s", 30))
        self.max_summary_tokens: int = int(
            self.config.get("max_summary_tokens", 500)
        )

    def apply_eager(self, event, *, store) -> bool:
        """Sync entry point — kept only to satisfy the abstract method.

        The real logic is in `apply_eager_async`. Calling this directly
        produces only the truncated fallback (no LLM upgrade) — useful
        for tests or for environments where async dispatch is bypassed.
        """
        if event.get("kind") != KIND_TOOL_RESULT:
            return False
        return self._truncate_inline(event, store) is not None

    async def apply_eager_async(self, ectx: EagerContext) -> bool:
        event = ectx.event
        if event.get("kind") != KIND_TOOL_RESULT:
            return False

        # Step 1: synchronous truncate. Always runs. This makes the event
        # safe-to-send before we do anything async, which means the tool
        # loop never blocks on us and the context budget is bounded even
        # if the upgrade never lands.
        body_before = event_body(event)
        truncate = self._truncate_inline(event, ectx.store, body=body_before)
        if truncate is None:
            # Body was small enough to skip — nothing to do.
            return False

        # If we don't have an LLM, we're done with just the truncate.
        # This is the same outcome as `tool_truncate` would produce.
        if ectx.llm is None:
            comp = event["meta"].setdefault("compaction", {})
            stamp = comp.setdefault("tool_summarize", {})
            stamp["state"] = "truncated_fallback"
            stamp["error"] = "no compaction LLM available"
            emit_event(
                _log,
                "compaction.eager.summarize_fallback",
                level=logging.WARNING,
                reason="no_llm",
                event_id=event.get("id"),
                session_id=ectx.session_id,
            )
            return True

        # Step 2: schedule the async LLM upgrade. We *do not* await it
        # here — the caller drains background tasks at turn boundary
        # with a small timeout (currently 2s in ui/app.py). Spawning
        # rather than awaiting is what keeps the tool loop fast.
        ectx.spawn(
            self._summarize_in_background(
                ectx=ectx,
                event=event,
                tool_name=(event.get("meta") or {}).get("name", ""),
                body=body_before,
            )
        )
        # `True` because step 1 modified `event['projected']`. The
        # background task may modify it again later via the callback.
        return True

    def _truncate_inline(self, event: dict, store, *, body: str | None = None):
        """Run the same truncate logic as `tool_truncate` and stamp meta.

        Returns the `TruncateResult` (or None if the body fit) so callers
        can short-circuit on small payloads. Stamps
        `meta.compaction.tool_summarize.state = "truncated_pending"` to
        signal that an LLM upgrade is queued; the background task flips
        this to `summarized` or `truncated_fallback`.
        """
        body_str = body if body is not None else event_body(event)
        result = _truncate_to_excerpt(
            body_str,
            store=store,
            max_tool_tokens=self.max_tool_tokens,
            head_tokens=self.head_tokens,
            tail_tokens=self.tail_tokens,
            marker_label="tool_summarize",
        )
        if result is None:
            return None
        event["projected"] = result.projected
        if event.get("meta") is None:
            event["meta"] = {}
        comp = event["meta"].setdefault("compaction", {})
        comp["tool_summarize"] = {
            "state": "truncated_pending",
            "original_bytes": result.original_bytes,
            "artifact": result.artifact_handle,
        }
        return result

    async def _summarize_in_background(
        self,
        *,
        ectx: EagerContext,
        event: dict,
        tool_name: str,
        body: str,
    ) -> None:
        """The actual LLM call. Runs as an `asyncio.Task`.

        On success: patches `event['projected']` to the summary, flips
        state to `summarized`, calls `ectx.on_event_updated(event)` so
        the session is re-saved.
        On any failure: leaves the truncated form in place, flips state
        to `truncated_fallback`, logs the reason. The truncated form is
        already on disk from step 1 so we never blow up the context
        budget.
        """
        comp = event["meta"].setdefault("compaction", {})
        stamp = comp.setdefault("tool_summarize", {})
        t0 = time.perf_counter()
        try:
            prompt = _build_prompt(
                tool_name=tool_name,
                body=body,
                max_summary_tokens=self.max_summary_tokens,
            )
            response = await asyncio.wait_for(
                ectx.llm.ainvoke(prompt),
                timeout=self.timeout_s,
            )
            text = _extract_text(response)
            if not text:
                raise RuntimeError("empty summary")

            usage = _extract_usage(response)
            artifact = stamp.get("artifact", "")
            header = (
                f"[summary of {tool_name or 'tool'} result, full payload at "
                f"{artifact}]"
            )
            event["projected"] = f"{header}\n{text}"
            stamp["state"] = "summarized"
            stamp["latency_ms"] = (time.perf_counter() - t0) * 1000.0
            stamp["summary_tokens"] = approx_tokens(text)
            if usage:
                stamp["llm_input_tokens"] = usage.get("input", 0)
                stamp["llm_output_tokens"] = usage.get("output", 0)

            emit_event(
                _log,
                "compaction.eager.summarized",
                event_id=event.get("id"),
                session_id=ectx.session_id,
                tool_name=tool_name,
                latency_ms=stamp["latency_ms"],
                summary_tokens=stamp["summary_tokens"],
                input_tokens=usage.get("input", 0) if usage else 0,
                output_tokens=usage.get("output", 0) if usage else 0,
            )
        except asyncio.TimeoutError:
            stamp["state"] = "truncated_fallback"
            stamp["error"] = f"timeout after {self.timeout_s:.0f}s"
            stamp["latency_ms"] = (time.perf_counter() - t0) * 1000.0
            emit_event(
                _log,
                "compaction.eager.summarize_fallback",
                level=logging.WARNING,
                reason="timeout",
                event_id=event.get("id"),
                session_id=ectx.session_id,
                latency_ms=stamp["latency_ms"],
            )
        except Exception as exc:  # noqa: BLE001 — explicit fallback path
            stamp["state"] = "truncated_fallback"
            stamp["error"] = f"{type(exc).__name__}: {exc}"
            stamp["latency_ms"] = (time.perf_counter() - t0) * 1000.0
            emit_event(
                _log,
                "compaction.eager.summarize_fallback",
                level=logging.WARNING,
                reason="exception",
                error_type=type(exc).__name__,
                event_id=event.get("id"),
                session_id=ectx.session_id,
                latency_ms=stamp["latency_ms"],
            )
        finally:
            cb = ectx.on_event_updated
            if cb is not None:
                try:
                    cb(event)
                except Exception:
                    # Save failure here is non-fatal — the truncated
                    # fallback is already on disk from step 1.
                    pass
