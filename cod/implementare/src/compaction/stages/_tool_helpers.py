"""
Shared helpers for the eager stages that rewrite oversized tool_result
events. The two stages that touch tool_result bodies — `tool_truncate`
and `tool_summarize` — both need the same head/tail-with-artifact-handle
projection; `tool_summarize` uses it as its always-on safety net before
attempting an LLM upgrade.

Centralising the helper here keeps the truncated form byte-identical
across the two stages, which is what makes "summarize falls back to
truncate" a true superset (the user gets either the same fallback they'd
have got from `tool_truncate` alone, or a strictly richer summary).
"""

from __future__ import annotations

from dataclasses import dataclass

from src.artifacts import ArtifactStore
from src.compaction.base import approx_tokens, stringify


@dataclass
class TruncateResult:
    """Outcome of `_truncate_to_excerpt` — None means the body was small enough."""

    projected: str
    artifact_handle: str
    original_bytes: int


def _truncate_to_excerpt(
    body: str,
    *,
    store: ArtifactStore,
    max_tool_tokens: int,
    head_tokens: int,
    tail_tokens: int,
    marker_label: str = "tool_truncate",
) -> TruncateResult | None:
    """Build a head + tail excerpt and stash the full payload as an artifact.

    Returns None when the body fits inside `max_tool_tokens` (caller does
    nothing). Otherwise the returned `projected` is what should be written
    to `event['projected']`, and the artifact handle is what
    `fetch_artifact` will use to re-hydrate the original.

    `marker_label` is interpolated into the visible "…[truncated by X,
    full payload at Y]…" marker so the agent (and humans reading the
    transcript) can tell which stage produced the truncation.
    """
    if approx_tokens(body) <= max_tool_tokens:
        return None

    ref = store.put(body, kind="tool_result", ext="txt")
    head_chars = head_tokens * 4
    tail_chars = tail_tokens * 4
    head = body[:head_chars]
    tail = body[-tail_chars:] if tail_chars > 0 else ""
    marker = (
        f"…[truncated by {marker_label}, full payload at {ref.handle} "
        f"({ref.bytes_} bytes)]…"
    )
    projected = f"{head}\n{marker}\n{tail}" if tail else f"{head}\n{marker}"
    return TruncateResult(
        projected=projected,
        artifact_handle=ref.handle,
        original_bytes=ref.bytes_,
    )


def event_body(event: dict) -> str:
    """Return the most up-to-date string body for a tool_result event.

    Stages run in pipeline order, each potentially writing to `projected`.
    A downstream stage that wants to operate on the *current* view (rather
    than the on-disk content) should read `projected` first.
    """
    return stringify(event.get("projected") or event.get("content"))


__all__ = ["TruncateResult", "_truncate_to_excerpt", "event_body"]
