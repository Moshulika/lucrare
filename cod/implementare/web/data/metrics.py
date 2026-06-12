"""Pure-function metrics derived from a session's event log."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from web.data.model_context import model_context_limits
from web.data.pricing import compute_cost, is_free_provider, model_price

# Default compaction trigger threshold — matches `config.yml`'s
# `compaction.trigger.threshold: 0.80`. Reading the live config from inside the
# read-only web layer would couple the dashboard to user state; the default is
# accurate for the vast majority of users and the chart slice is informational.
_DEFAULT_COMPACTION_THRESHOLD = 0.80

# Friendly category groupings for the context-window pie. Keys match the
# event-kind buckets used by `_SHARE_BUCKETS` in ui/sessions.py so the labels
# stay consistent with the shares doughnut.
_CONTEXT_CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("System prompt", ("system_prompt",)),
    ("User messages", ("human",)),
    ("Assistant responses", ("assistant_text",)),
    ("Thinking", ("assistant_thinking",)),
    ("Tool I/O", ("tool_call", "tool_result")),
    ("Subagents", ("subagent_call", "subagent_result")),
    ("Compaction summaries", ("compaction",)),
]


def _parse_ts(ts: str) -> float:
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _content_str(c: Any) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict):
                btype = b.get("type", "")
                # Multimodal user-attachment blocks (image / pdf / text).
                if btype == "attachment":
                    kind = b.get("kind", "?")
                    name = b.get("name", "?")
                    icon = (
                        "🖼" if kind == "image"
                        else "📄" if kind == "pdf"
                        else "📎"
                    )
                    parts.append(f"[{icon} {kind}: {name}]")
                    continue
                # Synthesized read_pdf_pages injection: each rendered page
                # is recorded as a compact `rendered_page` ref (sha-only).
                if btype == "rendered_page":
                    pdf_name = b.get("pdf_name", "?")
                    parts.append(f"[🖼 rendered: {pdf_name}]")
                    continue
                parts.append(b.get("text") or b.get("input") or "")
            else:
                parts.append(str(b))
        return "".join(parts)
    return str(c) if c is not None else ""


def _approx_tokens(text: str) -> int:
    """~4 chars-per-token estimate (matches src/compaction/base.approx_tokens)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def compute_all(session: dict) -> dict[str, Any]:
    events = session.get("events", []) or []
    metrics = session.get("metrics", {}) or {}
    provider = session.get("provider", "")
    model = session.get("model", "")

    return {
        "header": _header(session),
        "tokens": _tokens(metrics, provider, model),
        "context_window": _context_window(metrics, provider, model),
        "context_curve": _context_curve(events),
        "tool_calls": _tool_calls(events, metrics),
        "turns": _turn_anatomy(events),
        "compaction": _compaction(events, metrics),
        "latency": _latency(events),
        "model_switches": _model_switches(events),
        "thinking": _thinking(events),
        "events_summary": _events_summary(events),
        "subagents": _subagents(events, provider, model),
    }


def _context_window(metrics: dict, provider: str, model: str) -> dict[str, Any]:
    """Build a /context-style breakdown of the current context window.

    Slices:
      - one per friendly category (system prompt, user, assistant, thinking,
        tool I/O, subagents, compaction summaries) — sized by applying
        cumulative `by_kind` shares to the current LLM-reported context size,
        so the total matches `current_size`.
      - `max_output` — tokens reserved for the model's response.
      - `compaction_buffer` — the headroom kept above the trigger threshold
        (auto-compact will fire before this is consumed).
      - `free` — remaining headroom below the trigger threshold.

    The total `limit` is resolved in this order:
      1. `session.metrics.context.limit` if the session recorded it.
      2. The vendor-published max for (`provider`, `model`) — see
         `web/data/model_context.py`.
      3. Zero (chart renders an "unknown limit" placeholder).
    """
    ctx = (metrics.get("context") or {})
    recorded_limit = int(ctx.get("limit") or 0)
    current = int(ctx.get("current_size") or 0)
    by_kind = ((metrics.get("tokens") or {}).get("by_kind") or {})

    catalog_limit, catalog_max_output = model_context_limits(provider, model)
    limit = recorded_limit if recorded_limit > 0 else catalog_limit
    limit_source = (
        "session" if recorded_limit > 0
        else ("catalog" if catalog_limit > 0 else "unknown")
    )

    cat_cumulative: dict[str, int] = {}
    total_cumulative = 0
    for label, kinds in _CONTEXT_CATEGORIES:
        v = sum(int(by_kind.get(k) or 0) for k in kinds)
        if v > 0:
            cat_cumulative[label] = v
            total_cumulative += v

    # If current_size is missing (older sessions never wrote it), fall back to
    # the cumulative input total — it's a slight over-estimate but lets the
    # chart show *something* sensible instead of an empty pie.
    if current <= 0:
        current = int((metrics.get("tokens") or {}).get("input_total") or 0)
    if current <= 0:
        current = total_cumulative

    categories: dict[str, int] = {}
    if current > 0 and total_cumulative > 0:
        # Scale cumulative shares to fit the current context size. The last
        # non-zero slice absorbs rounding so the slices sum exactly to current.
        running = 0
        items = list(cat_cumulative.items())
        for i, (label, cum) in enumerate(items):
            if i == len(items) - 1:
                tokens = max(0, current - running)
            else:
                tokens = round(current * (cum / total_cumulative))
                running += tokens
            if tokens > 0:
                categories[label] = tokens
    else:
        # No LLM-reported size yet — fall back to cumulative so the chart
        # is still informative on a freshly resumed/empty session.
        categories = dict(cat_cumulative)

    threshold = _DEFAULT_COMPACTION_THRESHOLD
    used = sum(categories.values())
    if limit > 0:
        # Output reservation eats from the top of the window — like Claude
        # Code, we surface it as its own slice so users see why the model
        # can't consume the full nominal limit.
        max_output = min(int(catalog_max_output or 0), limit)
        compaction_buffer = max(0, round(limit * (1.0 - threshold)))
        # Trim reservations if their sum overflows the limit (happens when
        # `max_output` is large relative to `limit`).
        if max_output + compaction_buffer > limit:
            compaction_buffer = max(0, limit - max_output)
        free = max(0, limit - used - max_output - compaction_buffer)
    else:
        max_output = 0
        compaction_buffer = 0
        free = 0

    return {
        "limit": limit,
        "limit_source": limit_source,
        "current_size": current,
        "used_total": used,
        "threshold": threshold,
        "max_output": max_output,
        "compaction_buffer": compaction_buffer,
        "free": free,
        "categories": categories,
        "over_limit": limit > 0 and used > (limit - max_output - compaction_buffer),
    }


def _header(session: dict) -> dict[str, Any]:
    metrics = session.get("metrics", {}) or {}
    ctx = metrics.get("context", {}) or {}
    return {
        "id": session.get("id", ""),
        "short_id": (session.get("id") or "").split("-")[0],
        "provider": session.get("provider", ""),
        "model": session.get("model", ""),
        "model_params": session.get("model_params", {}) or {},
        "created_at": session.get("created_at", ""),
        "updated_at": session.get("updated_at", ""),
        "cwd": session.get("cwd", ""),
        "schema_version": session.get("schema_version", 0),
        "context_limit": ctx.get("limit", 0),
        "peak_size": ctx.get("peak_size", 0),
        "peak_utilization": ctx.get("peak_utilization", 0.0),
        "turns": metrics.get("turns", 0),
        "duration_seconds": _session_duration(session),
    }


def _session_duration(session: dict) -> float:
    a = _parse_ts(session.get("created_at", ""))
    b = _parse_ts(session.get("updated_at", ""))
    return max(0.0, b - a)


def _tokens(metrics: dict, provider: str, model: str) -> dict[str, Any]:
    t = (metrics.get("tokens") or {}).copy()
    by_kind = (t.get("by_kind") or {}).copy()
    shares = (t.get("shares") or {}).copy()
    cost = compute_cost(
        provider,
        model,
        int(t.get("input_total") or 0),
        int(t.get("output_total") or 0),
        int(t.get("cache_read_total") or 0),
    )
    p_in, p_out, p_cache = model_price(provider, model)
    return {
        "input_total": t.get("input_total", 0),
        "output_total": t.get("output_total", 0),
        "cache_read_total": t.get("cache_read_total", 0),
        "cache_write_total": t.get("cache_write_total", 0),
        "by_kind": by_kind,
        "shares": shares,
        "cost": cost,
        "price_per_million": {
            "input": p_in,
            "output": p_out,
            "cache_read": p_cache,
        },
        "free": is_free_provider(provider) or (p_in == 0 and p_out == 0),
    }


def _context_curve(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        out.append(
            {
                "ts": e.get("ts", ""),
                "kind": e.get("kind", ""),
                "context_size_after": e.get("context_size_after", 0),
                "event_id": e.get("id", ""),
            }
        )
    return out


def _tool_calls(events: list[dict], metrics: dict) -> dict[str, Any]:
    by_name: dict[str, dict[str, Any]] = {}
    pending: dict[str, dict[str, Any]] = {}
    durations: list[float] = []

    for e in events:
        kind = e.get("kind", "")
        meta = e.get("meta") or {}
        name = meta.get("name") or "?"
        call_id = meta.get("tool_call_id")

        if kind == "tool_call":
            entry = by_name.setdefault(
                name,
                {
                    "name": name,
                    "count": 0,
                    "total_result_tokens": 0,
                    "durations_ms": [],
                },
            )
            entry["count"] += 1
            if call_id:
                pending[call_id] = {
                    "name": name,
                    "ts": _parse_ts(e.get("ts", "")),
                }
        elif kind == "tool_result":
            entry = by_name.setdefault(
                name,
                {
                    "name": name,
                    "count": 0,
                    "total_result_tokens": 0,
                    "durations_ms": [],
                },
            )
            # Approximate result size by stored tokens or content length.
            tok = e.get("tokens") or {}
            sz = int(tok.get("input") or 0) + int(tok.get("output") or 0)
            if sz == 0:
                sz = max(1, len(_content_str(e.get("content"))) // 4)
            entry["total_result_tokens"] += sz
            if call_id and call_id in pending:
                start = pending.pop(call_id)["ts"]
                end = _parse_ts(e.get("ts", ""))
                if start and end >= start:
                    dur_ms = (end - start) * 1000.0
                    entry["durations_ms"].append(dur_ms)
                    durations.append(dur_ms)

    # Per-tool stats
    rows = []
    for name, e in by_name.items():
        ds = e["durations_ms"]
        rows.append(
            {
                "name": name,
                "count": e["count"],
                "total_result_tokens": e["total_result_tokens"],
                "p50_ms": _percentile(ds, 50),
                "p95_ms": _percentile(ds, 95),
                "max_ms": max(ds) if ds else 0.0,
                "mean_ms": (sum(ds) / len(ds)) if ds else 0.0,
            }
        )
    rows.sort(key=lambda r: r["count"], reverse=True)

    by_source: dict[str, int] = {}
    for e in events:
        if e.get("kind") == "tool_call":
            meta = e.get("meta") or {}
            src = meta.get("source")
            if not src:
                # Backfill for sessions recorded before the writer started
                # stamping `meta.source`. MCP tools follow the convention
                # `<server>__<tool>`; everything else is a built-in.
                name = meta.get("name") or ""
                src = ("mcp:" + name.split("__", 1)[0]) if "__" in name else "builtin"
            by_source[src] = by_source.get(src, 0) + 1

    return {
        "total_count": (metrics.get("tool_calls") or {}).get(
            "count", sum(r["count"] for r in rows)
        ),
        "unique_tools": len(rows),
        "by_name": rows,
        "by_source": by_source,
        "overall_p50_ms": _percentile(durations, 50),
        "overall_p95_ms": _percentile(durations, 95),
    }


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def _turn_anatomy(events: list[dict]) -> list[dict]:
    by_turn: dict[str, dict[str, Any]] = {}
    for e in events:
        tid = e.get("turn_id")
        if not tid:
            continue
        bucket = by_turn.setdefault(
            tid,
            {
                "turn_id": tid,
                "first_ts": e.get("ts", ""),
                "last_ts": e.get("ts", ""),
                "kinds": {},
                "tokens_by_kind": {},
                "tool_calls": 0,
                "human_preview": "",
            },
        )
        bucket["last_ts"] = e.get("ts", "")
        kind = e.get("kind", "")
        bucket["kinds"][kind] = bucket["kinds"].get(kind, 0) + 1
        if kind == "tool_call":
            bucket["tool_calls"] += 1
        # Per-kind token contribution (reported tokens > 0 wins; else estimate).
        tok = e.get("tokens") or {}
        reported = int(tok.get("input") or 0) + int(tok.get("output") or 0)
        if reported > 0:
            sz = reported
        else:
            sz = max(1, len(_content_str(e.get("content"))) // 4)
        bucket["tokens_by_kind"][kind] = bucket["tokens_by_kind"].get(kind, 0) + sz
        if kind == "human" and not bucket["human_preview"]:
            bucket["human_preview"] = _content_str(e.get("content"))[:80]

    rows = []
    for tid, b in by_turn.items():
        a = _parse_ts(b["first_ts"])
        z = _parse_ts(b["last_ts"])
        rows.append(
            {
                **b,
                "duration_seconds": max(0.0, z - a),
            }
        )
    rows.sort(key=lambda r: r["first_ts"])
    return rows


def _compaction(events: list[dict], metrics: dict) -> dict[str, Any]:
    threshold = list(metrics.get("compactions") or [])
    eager = dict(metrics.get("compaction_eager") or {})
    inline = []
    for e in events:
        if e.get("kind") == "compaction":
            meta = e.get("meta") or {}
            inline.append(
                {
                    "ts": e.get("ts", ""),
                    "before": meta.get("before_size", 0),
                    "after": meta.get("after_size", 0),
                    "freed": meta.get("freed", 0),
                    "removed_events": len(meta.get("removed_event_ids") or []),
                    "summary": _content_str(e.get("content"))[:200],
                }
            )
    return {
        "threshold_passes": threshold,
        "eager_stages": eager,
        "inline_events": inline,
        "any_compaction": bool(threshold or eager or inline),
    }


def _latency(events: list[dict]) -> dict[str, Any]:
    """Best-effort inference latency.

    Prefers explicit fields if present (request_started_at, duration_ms,
    time_to_first_token_ms). Falls back to ts-delta from the predecessor
    event, marking those as polluted by user think-time when applicable.
    """
    samples: list[dict[str, Any]] = []
    prev_ts = 0.0
    prev_kind: str | None = None
    for e in events:
        ts = _parse_ts(e.get("ts", ""))
        kind = e.get("kind", "")
        if kind in ("assistant_text", "assistant_thinking"):
            tok = e.get("tokens") or {}
            inp = int(tok.get("input") or 0)
            out_tok = int(tok.get("output") or 0)
            duration_ms = e.get("duration_ms")
            ttft_ms = e.get("time_to_first_token_ms")
            req_started = e.get("request_started_at")
            polluted = False
            if duration_ms is None:
                if req_started:
                    duration_ms = max(0.0, (ts - _parse_ts(req_started)) * 1000.0)
                elif prev_ts > 0:
                    duration_ms = max(0.0, (ts - prev_ts) * 1000.0)
                    polluted = prev_kind == "human"
            tps = None
            if duration_ms and out_tok > 0:
                tps = out_tok / (duration_ms / 1000.0)
            samples.append(
                {
                    "ts": e.get("ts", ""),
                    "kind": kind,
                    "input_tokens": inp,
                    "output_tokens": out_tok,
                    "duration_ms": duration_ms or 0,
                    "ttft_ms": ttft_ms,
                    "tokens_per_sec": tps,
                    "polluted_by_user_think_time": polluted,
                }
            )
        prev_ts = ts
        prev_kind = kind

    durations = [s["duration_ms"] for s in samples if s["duration_ms"]]
    ttfts = [s["ttft_ms"] for s in samples if s["ttft_ms"]]
    return {
        "samples": samples,
        "p50_ms": _percentile(durations, 50),
        "p95_ms": _percentile(durations, 95),
        "ttft_p50_ms": _percentile(ttfts, 50),
        "ttft_p95_ms": _percentile(ttfts, 95),
    }


def _model_switches(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        if e.get("kind") == "model_switch":
            meta = e.get("meta") or {}
            out.append(
                {
                    "ts": e.get("ts", ""),
                    "from": meta.get("from"),
                    "to": meta.get("to"),
                    "summary": _content_str(e.get("content"))[:200],
                }
            )
    return out


def _thinking(events: list[dict]) -> dict[str, Any]:
    blocks = [e for e in events if e.get("kind") == "assistant_thinking"]
    total_chars = sum(len(_content_str(e.get("content"))) for e in blocks)
    return {
        "block_count": len(blocks),
        "total_chars": total_chars,
        "avg_chars": (total_chars / len(blocks)) if blocks else 0,
    }


def _events_summary(events: list[dict]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    for e in events:
        k = e.get("kind", "")
        by_kind[k] = by_kind.get(k, 0) + 1
    return {"total": len(events), "by_kind": by_kind}


def _subagents(events: list[dict], provider: str, model: str) -> dict[str, Any]:
    """Aggregate KIND_SUBAGENT_RESULT events into per-type stats + per-invocation
    rows + totals. Pair calls↔results by `tool_call_id` so each invocation
    carries both its prompt (call.content) and its final report (result.content).

    Returns a stable shape even when there were no spawns — the dashboard
    hides the card on `count == 0`.
    """
    by_type: dict[str, dict[str, Any]] = {}
    depth_dist: dict[int, int] = {}
    invocations: list[dict[str, Any]] = []
    total_count = 0
    timed_out = 0
    total_input = 0
    total_output = 0
    total_cache_read = 0
    # Tokens the orchestrator actually received back (the final report text
    # per invocation, approximated by chars/4). Everything the subagent
    # processed internally beyond that is "saved" from orchestrator context.
    total_returned_to_orchestrator = 0

    # Count spawn_subagent tool calls so we can flag "attempted but errored
    # before subagent events were written" — diagnostic for tool-framework
    # failures (e.g. InjectedToolCallId envelope misuse) where the tool body
    # never reached the line that stashes the structured result.
    spawn_attempts = sum(
        1
        for e in events
        if e.get("kind") == "tool_call"
        and (e.get("meta") or {}).get("name") == "spawn_subagent"
    )

    # Index subagent_call events by tool_call_id so the matching result can
    # pull the prompt without scanning all events again.
    calls_by_id: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.get("kind") != "subagent_call":
            continue
        meta = e.get("meta") or {}
        tcid = meta.get("tool_call_id")
        if tcid:
            calls_by_id[tcid] = e

    for e in events:
        if e.get("kind") != "subagent_result":
            continue
        meta = e.get("meta") or {}
        stype = meta.get("subagent_type") or "?"
        tok = e.get("tokens") or {}
        inp = int(tok.get("input") or 0)
        out = int(tok.get("output") or 0)
        cr = int(tok.get("cache_read") or 0)
        cw = int(tok.get("cache_write") or 0)
        dur = float(e.get("duration_ms") or 0.0)
        depth = int(meta.get("depth") or 1)
        depth_dist[depth] = depth_dist.get(depth, 0) + 1
        timed_out_flag = bool(meta.get("timed_out"))
        if timed_out_flag:
            timed_out += 1

        bucket = by_type.setdefault(
            stype,
            {
                "type": stype,
                "count": 0,
                "input_total": 0,
                "output_total": 0,
                "cache_read_total": 0,
                "durations_ms": [],
                "timed_out": 0,
            },
        )
        bucket["count"] += 1
        bucket["input_total"] += inp
        bucket["output_total"] += out
        bucket["cache_read_total"] += cr
        if dur > 0:
            bucket["durations_ms"].append(dur)
        if timed_out_flag:
            bucket["timed_out"] += 1

        # Per-invocation row. Paired call carries the prompt the parent
        # passed to spawn_subagent.
        tcid = meta.get("tool_call_id") or ""
        paired = calls_by_id.get(tcid) if tcid else None
        prompt_text = _content_str(paired.get("content")) if paired else ""
        output_text = _content_str(e.get("content"))
        returned_tokens = _approx_tokens(output_text)
        total_returned_to_orchestrator += returned_tokens
        inv_cost = compute_cost(provider, model, inp, out, cr)
        invocations.append(
            {
                "id": e.get("id") or "",
                "ts": e.get("ts") or "",
                "type": stype,
                "depth": depth,
                "iterations": int(meta.get("iterations") or 0),
                "tool_call_count": int(meta.get("tool_call_count") or 0),
                "timed_out": timed_out_flag,
                "error": meta.get("error"),
                "duration_ms": dur,
                "prompt": prompt_text,
                "output": output_text,
                "tokens": {
                    "input": inp,
                    "output": out,
                    "cache_read": cr,
                    "cache_write": cw,
                    "returned_to_orchestrator": returned_tokens,
                },
                "cost": inv_cost,
                "tool_call_id": tcid,
            }
        )

        total_count += 1
        total_input += inp
        total_output += out
        total_cache_read += cr

    rows: list[dict[str, Any]] = []
    for stype, b in by_type.items():
        ds = b["durations_ms"]
        cost = compute_cost(
            provider, model, b["input_total"], b["output_total"], b["cache_read_total"]
        )
        rows.append(
            {
                "type": stype,
                "count": b["count"],
                "input_total": b["input_total"],
                "output_total": b["output_total"],
                "cache_read_total": b["cache_read_total"],
                "cost": cost,
                "mean_ms": (sum(ds) / len(ds)) if ds else 0.0,
                "p95_ms": _percentile(ds, 95),
                "max_ms": max(ds) if ds else 0.0,
                "timed_out": b["timed_out"],
            }
        )
    rows.sort(key=lambda r: r["count"], reverse=True)

    total_cost = compute_cost(
        provider, model, total_input, total_output, total_cache_read
    )

    # `errored_spawns` is the gap between `spawn_subagent` tool invocations
    # and recorded subagent_result events. A non-zero value means the tool
    # was called but the body never produced a structured result — usually
    # a tool-framework error surfaced as the ToolMessage content.
    errored_spawns = max(0, spawn_attempts - total_count)

    # Context savings: the subagent's LLM calls processed (input+output)
    # tokens that would have hit the orchestrator's context if inlined. Only
    # the final report text (returned_to_orchestrator) actually made it
    # back. The delta is tokens kept out of the orchestrator's context.
    subagent_processed_total = total_input + total_output
    tokens_saved = max(0, subagent_processed_total - total_returned_to_orchestrator)

    return {
        "count": total_count,
        "timed_out": timed_out,
        "by_type": rows,
        "depth_distribution": depth_dist,
        "input_total": total_input,
        "output_total": total_output,
        "cache_read_total": total_cache_read,
        "cost": total_cost,
        "invocations": invocations,
        "spawn_attempts": spawn_attempts,
        "errored_spawns": errored_spawns,
        "subagent_processed_total": subagent_processed_total,
        "returned_to_orchestrator_total": total_returned_to_orchestrator,
        "tokens_saved": tokens_saved,
    }
