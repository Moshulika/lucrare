"""
Lightweight in-memory tool_result truncation for subagent loops.

Subagent message buffers live entirely in memory inside `_drive_loop` — they
don't get persisted as session events, so the full `src/compaction` pipeline
(which is `Session`-bound and writes to disk) doesn't apply. We still want
oversized `ToolMessage` bodies to be trimmed eagerly so the next LLM call in
the child loop doesn't blow context.

This helper is intentionally minimal: head + tail excerpt with a visible
marker, no artifact spill. The full body is gone after truncation — but since
the child is the only consumer of its own buffer and produces just a final
text report to the parent, that's an acceptable trade-off. Compare against
`src/compaction/stages/_tool_helpers.py:_truncate_to_excerpt`, which keeps
the original in an ArtifactStore for `fetch_artifact` recovery; the parent
agent still has access to that path.
"""

from __future__ import annotations

from langchain_core.messages import ToolMessage

from src.compaction.base import approx_tokens


def truncate_oversized_tool_results(
    messages: list,
    *,
    max_tool_tokens: int,
    head_tokens: int = 800,
    tail_tokens: int = 400,
) -> int:
    """Mutate `messages` in place — replace oversized ToolMessage bodies
    with a head + tail excerpt and a visible truncation marker.

    Returns the number of messages that were truncated. Set
    `max_tool_tokens` to 0 to disable (no-op).
    """
    if max_tool_tokens <= 0:
        return 0

    head_chars = head_tokens * 4
    tail_chars = tail_tokens * 4
    truncated = 0

    for m in messages:
        if not isinstance(m, ToolMessage):
            continue
        body = m.content if isinstance(m.content, str) else str(m.content)
        if approx_tokens(body) <= max_tool_tokens:
            continue
        head = body[:head_chars]
        tail = body[-tail_chars:] if tail_chars > 0 else ""
        marker = (
            f"…[truncated in-subagent: {len(body)} chars trimmed to "
            f"head+tail excerpt; original not retrievable in this child loop]…"
        )
        m.content = f"{head}\n{marker}\n{tail}" if tail else f"{head}\n{marker}"
        truncated += 1

    return truncated


__all__ = ["truncate_oversized_tool_results"]