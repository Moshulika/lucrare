"""
vibe-cli — Per-project session store.

A session is an append-only log of *events* (human input, assistant text,
assistant thinking, tool calls, tool results, compactions, model switches).
Token usage is captured per event so we can answer questions like:

    - How big was the context window after event N?
    - What share of context went to tools vs. thinking vs. assistant?
    - When did compaction events fire and how much did they free?

Files live at ~/.vibe-cli/sessions/<encoded-cwd>/<uuid7>.json. Filenames
are UUIDv7 so they sort by creation time and stay unique across machines.

Resuming a session projects its event log back into LangChain messages
that the LangGraph agent can ingest.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from uuid_utils import uuid7

from ui.paths import SESSIONS_DIR, encode_cwd, project_dir, session_backup_dir


# ---------------------------------------------------------
# Event kinds
# ---------------------------------------------------------
KIND_HUMAN = "human"
KIND_ASSISTANT_TEXT = "assistant_text"
KIND_ASSISTANT_THINKING = "assistant_thinking"
KIND_TOOL_CALL = "tool_call"
KIND_TOOL_RESULT = "tool_result"
KIND_SYSTEM_PROMPT = "system_prompt"
KIND_COMPACTION = "compaction"
KIND_MODEL_SWITCH = "model_switch"
KIND_SUBAGENT_CALL = "subagent_call"
KIND_SUBAGENT_RESULT = "subagent_result"

# Which kinds count toward which "share" bucket in metrics.tokens.shares.
_SHARE_BUCKETS = {
    "tools": (KIND_TOOL_CALL, KIND_TOOL_RESULT),
    "thinking": (KIND_ASSISTANT_THINKING,),
    "assistant": (KIND_ASSISTANT_TEXT,),
    "human": (KIND_HUMAN,),
    "system": (KIND_SYSTEM_PROMPT,),
    "compaction": (KIND_COMPACTION,),
    "subagents": (KIND_SUBAGENT_CALL, KIND_SUBAGENT_RESULT),
}

SCHEMA_VERSION = 1


class _ToolPairingError(Exception):
    """Raised by the persistent projection when tool_call ↔ tool_result
    pairing is inconsistent. Caught by `to_messages()` which then falls back
    to the ephemeral projection so resume always succeeds."""


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------
def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _new_id() -> str:
    return str(uuid7())


def _approx_tokens(text: str) -> int:
    """Cheap byte-pair estimate; ~4 chars per token. Empty text → 0."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def new_turn_id() -> str:
    """Public helper so callers can stamp a turn before recording its events."""
    return _new_id()


def _emit_session_event(sess, evt: dict) -> None:
    """Emit a low-noise structured log line per appended event.

    DEBUG-level so it stays out of the text stream by default. Carries
    only kind + content size + correlation IDs — no raw text.
    """
    import logging as _logging

    from src.logging_setup import emit_event as _emit, get_logger as _get_logger

    content = evt.get("content")
    if isinstance(content, str):
        size = len(content)
    elif content is None:
        size = 0
    else:
        try:
            size = len(str(content))
        except Exception:
            size = 0
    _emit(
        _get_logger("session"),
        "session.event",
        level=_logging.DEBUG,
        kind=evt.get("kind"),
        event_id=evt.get("id"),
        bytes=size,
        turn_id=evt.get("turn_id"),
        llm_call_id=evt.get("llm_call_id"),
        session_id=sess.id,
    )


def _empty_tokens() -> dict:
    return {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "estimated": True,
    }


def _empty_metrics() -> dict:
    return {
        "tokens": {
            "input_total": 0,
            "output_total": 0,
            "cache_read_total": 0,
            "cache_write_total": 0,
            "by_kind": {},
            "shares": {},
        },
        "context": {
            "current_size": 0,
            "peak_size": 0,
            "limit": 0,
            "peak_utilization": 0.0,
        },
        "compactions": [],
        # Per-eager-stage rollup, parallel to `compactions` (which tracks
        # threshold passes). Each entry is keyed by stage name and holds:
        #   { "count": int, "original_bytes_total": int, "last_event_id": str }
        # `original_bytes_total` is the sum of `meta.compaction.<stage>.original_bytes`
        # (or `original_chars` for stages that don't store a payload artifact).
        "compaction_eager": {},
        "tool_calls": {"count": 0, "by_name": {}},
        "turns": 0,
    }


def _projected_content(evt: dict) -> Any:
    """Return the event's projected (compacted) body if present, else its content.

    Eager compaction stages write a `projected` field alongside the immutable
    `content`. The original `content` is the source of truth on disk; the
    `projected` form is what gets sent to the LLM and resumed-from.
    """
    if "projected" in evt and evt["projected"] is not None:
        return evt["projected"]
    return evt.get("content")


def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict):
                out.append(block.get("text") or block.get("input") or "")
            else:
                out.append(str(block))
        return "".join(out)
    return str(content)


# ---------------------------------------------------------
# Session
# ---------------------------------------------------------
class Session:
    """An append-only event log for one chat session in one project."""

    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data

    # ----- identity ------------------------------------------------
    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def short_id(self) -> str:
        return self.id.split("-")[0]

    @property
    def events(self) -> list[dict]:
        return self.data["events"]

    @property
    def metrics(self) -> dict:
        return self.data["metrics"]

    @property
    def updated_at(self) -> str:
        return self.data.get("updated_at", "")

    @property
    def created_at(self) -> str:
        return self.data.get("created_at", "")

    @property
    def provider(self) -> str:
        return self.data.get("provider", "")

    @property
    def model(self) -> str:
        return self.data.get("model", "")

    # ----- construction --------------------------------------------
    @classmethod
    def create(
        cls,
        provider: str,
        model: str,
        cwd: Path | None = None,
        context_limit: int = 0,
        model_params: dict | None = None,
    ) -> "Session":
        sid = _new_id()
        d = project_dir(cwd)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{sid}.json"
        now = _now_iso()
        data = {
            "id": sid,
            "schema_version": SCHEMA_VERSION,
            "cwd": str((cwd or Path.cwd()).resolve()),
            "created_at": now,
            "updated_at": now,
            "provider": provider,
            "model": model,
            "model_params": model_params or {},
            "events": [],
            "metrics": _empty_metrics(),
        }
        data["metrics"]["context"]["limit"] = context_limit
        s = cls(path, data)
        s.save()
        return s

    @classmethod
    def load(cls, path: Path) -> "Session":
        data = json.loads(path.read_text())
        data.setdefault("metrics", _empty_metrics())
        data.setdefault("events", [])
        return cls(path, data)

    # ----- persistence ---------------------------------------------
    def save(self) -> None:
        """Atomic write — temp file + rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)

    def snapshot_backup(self, keep: int | None = None) -> Path:
        """Snapshot the current session file into session_backups/.

        Called by the compaction pipeline immediately *before* any
        threshold-mode pass mutates the projected view, so we can later
        diff "what the LLM actually saw" against "what really happened."

        Args:
            keep: If set, retain only the `keep` most recent snapshots for
                this session (oldest get unlinked). None = keep all.

        Returns:
            Path to the snapshot file.
        """
        # Make sure the live file is on disk before we copy it.
        if not self.path.exists():
            self.save()
        cwd = self.data.get("cwd")
        backup_dir = session_backup_dir(
            self.id, cwd=Path(cwd) if cwd else None
        )
        backup_dir.mkdir(parents=True, exist_ok=True)
        # Filesystem-safe timestamp.
        ts = _now_iso().replace(":", "-").replace(".", "-")
        dest = backup_dir / f"pre-compaction-{ts}.json"
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(self.path.read_bytes())
        tmp.replace(dest)
        if keep is not None and keep > 0:
            existing = sorted(backup_dir.glob("pre-compaction-*.json"))
            for old in existing[:-keep]:
                try:
                    old.unlink()
                except OSError:
                    pass
        return dest

    # ----- mutation -------------------------------------------------
    def append_event(
        self,
        kind: str,
        content: Any,
        *,
        tokens: dict | None = None,
        parent_id: str | None = None,
        meta: dict | None = None,
        turn_id: str | None = None,
        llm_call_id: str | None = None,
        request_started_at: str | None = None,
        duration_ms: float | None = None,
        time_to_first_token_ms: float | None = None,
    ) -> dict:
        evt: dict[str, Any] = {
            "id": _new_id(),
            "ts": _now_iso(),
            "kind": kind,
            "content": content,
            "tokens": {**_empty_tokens(), **(tokens or {})},
            "parent_id": parent_id,
            "turn_id": turn_id,
            "llm_call_id": llm_call_id,
            "meta": meta or {},
        }
        if request_started_at is not None:
            evt["request_started_at"] = request_started_at
        if duration_ms is not None:
            evt["duration_ms"] = float(duration_ms)
        if time_to_first_token_ms is not None:
            evt["time_to_first_token_ms"] = float(time_to_first_token_ms)
        self.events.append(evt)
        self._update_metrics(evt)
        self.data["updated_at"] = evt["ts"]
        self.save()
        _emit_session_event(self, evt)
        return evt

    def record_compaction(
        self,
        before_size: int,
        after_size: int,
        removed_event_ids: list[str],
        summary: str,
    ) -> dict:
        """Convenience for future compaction logic — not invoked yet."""
        evt = self.append_event(
            KIND_COMPACTION,
            summary,
            tokens={"input": after_size, "output": 0, "estimated": True},
            meta={
                "before_size": before_size,
                "after_size": after_size,
                "removed_event_ids": removed_event_ids,
                "freed": before_size - after_size,
            },
        )
        self.metrics["compactions"].append(
            {
                "ts": evt["ts"],
                "before": before_size,
                "after": after_size,
                "freed": before_size - after_size,
                "removed_events": len(removed_event_ids),
                "event_id": evt["id"],
            }
        )
        self.metrics["context"]["current_size"] = after_size
        self.save()
        return evt

    # ----- metrics --------------------------------------------------
    def _update_metrics(self, evt: dict) -> None:
        m = self.metrics
        t = evt["tokens"]
        toks = m["tokens"]

        toks["input_total"] += int(t.get("input") or 0)
        toks["output_total"] += int(t.get("output") or 0)
        toks["cache_read_total"] += int(t.get("cache_read") or 0)
        toks["cache_write_total"] += int(t.get("cache_write") or 0)

        # Per-kind contribution: prefer reported tokens, else estimate from content.
        reported = int(t.get("input") or 0) + int(t.get("output") or 0)
        size = reported if reported > 0 else _approx_tokens(_stringify(evt["content"]))
        toks["by_kind"][evt["kind"]] = toks["by_kind"].get(evt["kind"], 0) + size

        # Recompute shares.
        total = sum(toks["by_kind"].values()) or 1
        toks["shares"] = {
            bucket: sum(toks["by_kind"].get(k, 0) for k in kinds) / total
            for bucket, kinds in _SHARE_BUCKETS.items()
        }

        # Authoritative context size: total tokens reported on the latest LLM
        # call (an assistant_text or assistant_thinking event with input>0).
        if evt["kind"] in (KIND_ASSISTANT_TEXT, KIND_ASSISTANT_THINKING) and (
            int(t.get("input") or 0) > 0
        ):
            cur = int(t.get("input") or 0) + int(t.get("output") or 0)
            m["context"]["current_size"] = cur
            if cur > m["context"].get("peak_size", 0):
                m["context"]["peak_size"] = cur
            limit = m["context"].get("limit") or 0
            m["context"]["peak_utilization"] = (
                m["context"]["peak_size"] / limit if limit else 0.0
            )

        evt["context_size_after"] = m["context"]["current_size"]

        if evt["kind"] == KIND_TOOL_CALL:
            tc = m["tool_calls"]
            tc["count"] += 1
            name = (evt.get("meta") or {}).get("name") or "?"
            tc["by_name"][name] = tc["by_name"].get(name, 0) + 1

        if evt["kind"] == KIND_HUMAN:
            m["turns"] += 1

    # ----- projections ---------------------------------------------
    def first_human_preview(self, n: int = 60) -> str:
        for e in self.events:
            if e["kind"] == KIND_HUMAN:
                return _stringify(e["content"])[:n]
        return ""

    def to_messages(
        self,
        *,
        tool_persistence: bool = False,
        provider: str | None = None,
        capability_filter=None,
    ) -> list[BaseMessage]:
        """Project events back into LangChain messages for resume.

        Two projection modes, controlled by `tool_persistence`:

        - `tool_persistence=False` (default, ephemeral mode): one HumanMessage
          + one AIMessage per turn, where the AIMessage holds the final
          assistant text from that turn. Tool calls / results / thinking stay
          in the event log for analytics but are dropped from the projection.
          This matches vibe-cli's historical behavior and is provider-safe
          (re-submitting the list to any provider works because there are no
          tool message format concerns).

        - `tool_persistence=True` (persistent mode): tool calls and tool
          results are projected back as `AIMessage(tool_calls=[...])` plus
          matching `ToolMessage` entries, so the resumed model sees the same
          tool history it produced live. Events are grouped by `llm_call_id`
          so each LLM invocation maps to one AIMessage carrying its text and
          its tool_calls. ToolMessages are paired by `tool_call_id`.

          Validation: every tool_call must have exactly one matching
          tool_result and vice versa. If pairing fails (orphaned id,
          duplicate id, missing result), the projection silently falls back
          to the ephemeral shape and emits a warning log entry. Cross-provider
          replay can produce malformed sessions; this fallback keeps resume
          working even when persistence has gone wrong.

        Compaction-aware behavior (both modes):
          - Events whose `id` is listed in any KIND_COMPACTION event's
            `meta.removed_event_ids` are skipped.
          - Each event's `projected` field (set by eager compaction stages
            like tool_truncate / ref_substitute) is preferred over its
            original `content` when emitting messages.
          - KIND_COMPACTION events themselves emit a SystemMessage carrying
            their summary, in-place (chronologically) so that re-submitted
            history reflects what the LLM was actually shown post-compaction.
        """
        # Collect every event-id that any compaction event marked removed.
        removed_ids: set[str] = set()
        for e in self.events:
            if e["kind"] == KIND_COMPACTION:
                ids = (e.get("meta") or {}).get("removed_event_ids") or []
                removed_ids.update(ids)

        if tool_persistence:
            try:
                return self._to_messages_persistent(
                    removed_ids,
                    provider=provider,
                    capability_filter=capability_filter,
                )
            except _ToolPairingError as exc:
                # Malformed tool_call ↔ tool_result pairing. Fall back to the
                # ephemeral projection so resume always succeeds, and log a
                # warning so the user can investigate.
                import logging as _logging

                from src.logging_setup import emit_event as _emit, get_logger as _get_logger

                _emit(
                    _get_logger("session"),
                    "session.tool_pairing_fallback",
                    level=_logging.WARNING,
                    session_id=self.id,
                    reason=str(exc),
                )

        return self._to_messages_ephemeral(
            removed_ids,
            provider=provider,
            capability_filter=capability_filter,
        )

    def _project_human_content(
        self,
        content,
        *,
        provider: str | None,
        capability_filter,
    ):
        """Re-shape a stored KIND_HUMAN content payload for use as a HumanMessage.

        - Plain strings round-trip identically.
        - List-shaped content with embedded "attachment" blocks is expanded
          into provider-native multimodal blocks via `build_human_content`.
          If `provider` is None we fall back to stringifying (best-effort
          for compaction / metrics paths that don't care about shape).
        """
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return _stringify(content)

        # Reconstruct text + Attachment objects from the stored blocks.
        text_parts: list[str] = []
        attachments: list = []
        try:
            from src.attachments import Attachment as _Attachment
        except Exception:
            return _stringify(content)
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(str(block.get("text") or ""))
            elif btype == "attachment":
                try:
                    attachments.append(_Attachment.from_block(block))
                except Exception:
                    continue

        text = "\n".join(t for t in text_parts if t)

        if provider is None:
            # No provider context — return as plain text, mentioning the
            # attachments by name so the model still has *something*.
            if attachments:
                tail = "\n".join(
                    f"[attachment: {a.name} ({a.kind})]" for a in attachments
                )
                return f"{text}\n\n{tail}" if text else tail
            return text

        try:
            from src.multimodal import build_human_content
        except Exception:
            return text

        return build_human_content(
            text,
            attachments,
            provider,
            session_id=self.id,
            capability_filter=capability_filter,
        )

    def _to_messages_ephemeral(
        self,
        removed_ids: set[str],
        *,
        provider: str | None = None,
        capability_filter=None,
    ) -> list[BaseMessage]:
        """Original projection: HumanMessage + final AIMessage per turn."""
        msgs: list[BaseMessage] = []
        cur_turn: str | None = None
        cur_human = None  # type: ignore[assignment]
        cur_final_text: str = ""

        def flush() -> None:
            nonlocal cur_turn, cur_human, cur_final_text
            if cur_human is not None:
                msgs.append(HumanMessage(content=cur_human))
            if cur_final_text:
                msgs.append(AIMessage(content=cur_final_text))
            cur_turn = None
            cur_human = None
            cur_final_text = ""

        for e in self.events:
            if e["id"] in removed_ids:
                continue

            kind = e["kind"]
            raw = _projected_content(e)

            if kind == KIND_SYSTEM_PROMPT:
                flush()
                msgs.append(SystemMessage(content=_stringify(raw)))
                continue

            if kind == KIND_COMPACTION:
                flush()
                body = _stringify(raw)
                if body.strip():
                    msgs.append(SystemMessage(content=body))
                continue

            tid = e.get("turn_id")
            if tid is None:
                continue
            if tid != cur_turn:
                flush()
                cur_turn = tid

            if kind == KIND_HUMAN and cur_human is None:
                cur_human = self._project_human_content(
                    raw,
                    provider=provider,
                    capability_filter=capability_filter,
                )
            elif kind == KIND_ASSISTANT_TEXT and _stringify(raw).strip():
                cur_final_text = _stringify(raw)

        flush()
        return msgs

    def _to_messages_persistent(
        self,
        removed_ids: set[str],
        *,
        provider: str | None = None,
        capability_filter=None,
    ) -> list[BaseMessage]:
        """Projection that preserves tool calls and tool results.

        Walks events in chronological order. For each turn:
          - emit one HumanMessage (the first KIND_HUMAN of the turn)
          - group assistant-side events (KIND_ASSISTANT_TEXT, KIND_TOOL_CALL)
            by `llm_call_id`, in first-occurrence order
          - per group: emit one AIMessage carrying that call's text + any
            tool_calls; immediately follow with the matching ToolMessages
            (paired by tool_call_id, looked up across the whole turn so that
            interleaved tool execution maps cleanly onto LangChain's expected
            sequence: AI(tool_calls), Tool, Tool, AI(text-or-more-tool_calls)).

        Raises _ToolPairingError if any tool_call lacks a matching tool_result
        in the same turn (or vice versa). The caller falls back to ephemeral.
        """
        msgs: list[BaseMessage] = []

        # Build a tool_call_id → tool_result event index up front (whole
        # session). A tool_result may not always carry the same turn_id as
        # its tool_call — pairing by id is the source of truth.
        result_by_call_id: dict[str, dict] = {}
        for e in self.events:
            if e["id"] in removed_ids:
                continue
            if e["kind"] != KIND_TOOL_RESULT:
                continue
            tcid = (e.get("meta") or {}).get("tool_call_id") or ""
            if not tcid:
                raise _ToolPairingError(f"tool_result {e['id']} has no tool_call_id")
            if tcid in result_by_call_id:
                raise _ToolPairingError(f"duplicate tool_result for {tcid}")
            result_by_call_id[tcid] = e

        consumed_call_ids: set[str] = set()

        # Group by turn while preserving order.
        cur_turn: str | None = None
        turn_events: list[dict] = []

        def flush_turn() -> None:
            nonlocal cur_turn, turn_events
            if not turn_events:
                cur_turn = None
                turn_events = []
                return

            # 1. HumanMessage (multimodal-aware)
            for ev in turn_events:
                if ev["kind"] == KIND_HUMAN:
                    msgs.append(
                        HumanMessage(
                            content=self._project_human_content(
                                _projected_content(ev),
                                provider=provider,
                                capability_filter=capability_filter,
                            )
                        )
                    )
                    break

            # 2. Group assistant-side events by llm_call_id, preserving
            #    first-seen order.
            llm_call_order: list[str] = []
            llm_call_buckets: dict[str, list[dict]] = {}
            for ev in turn_events:
                if ev["kind"] not in (KIND_ASSISTANT_TEXT, KIND_TOOL_CALL):
                    continue
                lcid = ev.get("llm_call_id") or ""
                if lcid not in llm_call_buckets:
                    llm_call_buckets[lcid] = []
                    llm_call_order.append(lcid)
                llm_call_buckets[lcid].append(ev)

            for lcid in llm_call_order:
                bucket = llm_call_buckets[lcid]
                text = ""
                tool_calls: list[dict] = []
                for ev in bucket:
                    if ev["kind"] == KIND_ASSISTANT_TEXT:
                        # Last-write-wins if there are somehow multiple text
                        # events under one llm_call_id (shouldn't happen but
                        # be defensive).
                        text = _stringify(_projected_content(ev))
                    elif ev["kind"] == KIND_TOOL_CALL:
                        meta = ev.get("meta") or {}
                        tc_id = meta.get("tool_call_id") or ""
                        if not tc_id:
                            raise _ToolPairingError(
                                f"tool_call {ev['id']} has no tool_call_id"
                            )
                        args = _projected_content(ev)
                        if not isinstance(args, dict):
                            # eager compaction may stringify args; best-effort
                            # parse, otherwise wrap.
                            try:
                                import json as _json

                                parsed = _json.loads(args) if isinstance(args, str) else None
                                args = parsed if isinstance(parsed, dict) else {"_raw": args}
                            except Exception:
                                args = {"_raw": str(args)}
                        tool_calls.append(
                            {
                                "id": tc_id,
                                "name": meta.get("name") or "",
                                "args": args,
                            }
                        )

                if not text and not tool_calls:
                    continue

                msgs.append(AIMessage(content=text, tool_calls=tool_calls))

                for tc in tool_calls:
                    tr = result_by_call_id.get(tc["id"])
                    if tr is None:
                        raise _ToolPairingError(
                            f"tool_call {tc['id']} has no matching tool_result"
                        )
                    consumed_call_ids.add(tc["id"])
                    msgs.append(
                        ToolMessage(
                            content=_stringify(_projected_content(tr)),
                            tool_call_id=tc["id"],
                            name=(tr.get("meta") or {}).get("name") or "",
                        )
                    )

            cur_turn = None
            turn_events = []

        for e in self.events:
            if e["id"] in removed_ids:
                continue
            kind = e["kind"]

            if kind == KIND_SYSTEM_PROMPT:
                flush_turn()
                msgs.append(
                    SystemMessage(content=_stringify(_projected_content(e)))
                )
                continue

            if kind == KIND_COMPACTION:
                flush_turn()
                body = _stringify(_projected_content(e))
                if body.strip():
                    msgs.append(SystemMessage(content=body))
                continue

            tid = e.get("turn_id")
            if tid is None:
                continue
            if tid != cur_turn:
                flush_turn()
                cur_turn = tid

            turn_events.append(e)

        flush_turn()

        # Sanity check: every tool_result we have must have been consumed by
        # a tool_call. Orphaned tool_results would dangle without a parent
        # AIMessage and break re-submission.
        unconsumed = set(result_by_call_id.keys()) - consumed_call_ids
        if unconsumed:
            raise _ToolPairingError(
                f"orphan tool_result(s) for ids: {sorted(unconsumed)}"
            )

        return msgs


# ---------------------------------------------------------
# Module-level lookup helpers
# ---------------------------------------------------------
def list_sessions(cwd: Path | None = None) -> list[Session]:
    """All sessions in the current project, sorted by updated_at desc."""
    d = project_dir(cwd)
    if not d.exists():
        return []
    sessions: list[Session] = []
    for path in d.glob("*.json"):
        try:
            sessions.append(Session.load(path))
        except (json.JSONDecodeError, OSError, KeyError):
            continue
    sessions.sort(key=lambda s: s.updated_at, reverse=True)
    return sessions


def find_session(prefix: str, cwd: Path | None = None) -> Session | list[Session] | None:
    """Resolve a session by id or unique id-prefix in the current project.

    Returns:
        - Session: unique match
        - list[Session]: ambiguous matches (caller should disambiguate)
        - None: no match
    """
    if not prefix:
        return None
    matches = [s for s in list_sessions(cwd) if s.id.startswith(prefix)]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    return matches


def latest_session(cwd: Path | None = None) -> Session | None:
    sessions = list_sessions(cwd)
    return sessions[0] if sessions else None


__all__ = [
    "Session",
    "list_sessions",
    "find_session",
    "latest_session",
    "new_turn_id",
    "encode_cwd",
    "project_dir",
    "SESSIONS_DIR",
    "KIND_HUMAN",
    "KIND_ASSISTANT_TEXT",
    "KIND_ASSISTANT_THINKING",
    "KIND_TOOL_CALL",
    "KIND_TOOL_RESULT",
    "KIND_SYSTEM_PROMPT",
    "KIND_COMPACTION",
    "KIND_MODEL_SWITCH",
    "KIND_SUBAGENT_CALL",
    "KIND_SUBAGENT_RESULT",
]