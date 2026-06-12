"""
vibe-cli — ContextVars carrying correlation IDs across log records.

Set once at the boundary of a turn / tool call / event and the
`JsonFormatter` (in ``src.logging_setup``) picks them up automatically,
including across ``asyncio.create_task`` (ContextVars are copied into
new tasks).
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

current_session_id: ContextVar[str | None] = ContextVar(
    "vibe_session_id", default=None
)
current_turn_id: ContextVar[str | None] = ContextVar(
    "vibe_turn_id", default=None
)
current_event_id: ContextVar[str | None] = ContextVar(
    "vibe_event_id", default=None
)


@contextmanager
def log_turn(
    session_id: str | None = None,
    turn_id: str | None = None,
) -> Iterator[None]:
    """Bind a session/turn pair for the duration of the block."""
    s_token = current_session_id.set(session_id) if session_id is not None else None
    t_token = current_turn_id.set(turn_id) if turn_id is not None else None
    try:
        yield
    finally:
        if t_token is not None:
            current_turn_id.reset(t_token)
        if s_token is not None:
            current_session_id.reset(s_token)


@contextmanager
def log_event(event_id: str | None) -> Iterator[None]:
    """Bind an event id for the duration of the block."""
    if event_id is None:
        yield
        return
    token = current_event_id.set(event_id)
    try:
        yield
    finally:
        current_event_id.reset(token)


def bind_session_id(session_id: str | None) -> None:
    """Set ``current_session_id`` for the rest of the task. No reset.

    Use this when the active session changes (startup, /clear, /resume) so
    every subsequent log record on this task carries the new id without
    needing a context-manager block per call.
    """
    current_session_id.set(session_id)


def get_correlation() -> dict:
    """Snapshot the current correlation IDs (used by the JSON formatter)."""
    out: dict = {}
    sid = current_session_id.get()
    tid = current_turn_id.get()
    eid = current_event_id.get()
    if sid:
        out["session_id"] = sid
    if tid:
        out["turn_id"] = tid
    if eid:
        out["event_id"] = eid
    return out