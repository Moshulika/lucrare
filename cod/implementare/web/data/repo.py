"""Read-only filesystem access to ~/.vibe-cli/sessions and session_backups."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from web.config import SESSION_BACKUPS_DIR, SESSIONS_DIR


def list_projects() -> list[dict[str, Any]]:
    if not SESSIONS_DIR.exists():
        return []
    out = []
    for d in sorted(SESSIONS_DIR.iterdir()):
        if not d.is_dir():
            continue
        sess_files = list(d.glob("*.json"))
        if not sess_files:
            continue
        cwd = _decode_cwd_from_session(sess_files[0]) or _fallback_decode(d.name)
        last_updated = max((p.stat().st_mtime for p in sess_files), default=0)
        out.append(
            {
                "key": d.name,
                "cwd": cwd,
                "session_count": len(sess_files),
                "last_updated": last_updated,
            }
        )
    out.sort(key=lambda p: p["last_updated"], reverse=True)
    return out


def list_sessions(project_key: str) -> list[dict[str, Any]]:
    d = SESSIONS_DIR / project_key
    if not d.exists():
        return []
    out = []
    for path in d.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.append(_session_summary(data))
    out.sort(key=lambda s: s["updated_at"], reverse=True)
    return out


def load_session(project_key: str, session_id: str) -> dict[str, Any] | None:
    path = SESSIONS_DIR / project_key / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def list_pre_compaction_snapshots(
    project_key: str, session_id: str
) -> list[dict[str, Any]]:
    d = SESSION_BACKUPS_DIR / project_key / session_id
    if not d.exists():
        return []
    out = []
    for path in sorted(d.glob("pre-compaction-*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.append(
            {
                "filename": path.name,
                "events": len(data.get("events", [])),
                "metrics": data.get("metrics", {}),
                "updated_at": data.get("updated_at", ""),
            }
        )
    return out


def _session_summary(data: dict) -> dict[str, Any]:
    metrics = data.get("metrics", {}) or {}
    tokens = metrics.get("tokens", {}) or {}
    ctx = metrics.get("context", {}) or {}
    first_human = ""
    for e in data.get("events", []):
        if e.get("kind") == "human":
            c = e.get("content")
            first_human = (c if isinstance(c, str) else str(c))[:80]
            break
    return {
        "id": data.get("id", ""),
        "short_id": (data.get("id") or "").split("-")[0],
        "provider": data.get("provider", ""),
        "model": data.get("model", ""),
        "created_at": data.get("created_at", ""),
        "updated_at": data.get("updated_at", ""),
        "event_count": len(data.get("events", [])),
        "turns": metrics.get("turns", 0),
        "input_total": tokens.get("input_total", 0),
        "output_total": tokens.get("output_total", 0),
        "cache_read_total": tokens.get("cache_read_total", 0),
        "peak_size": ctx.get("peak_size", 0),
        "limit": ctx.get("limit", 0),
        "has_compaction": bool(metrics.get("compactions"))
        or bool(metrics.get("compaction_eager")),
        "first_human_preview": first_human,
        "tool_calls": (metrics.get("tool_calls") or {}).get("count", 0),
    }


def _decode_cwd_from_session(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return data.get("cwd") or None


def _fallback_decode(encoded: str) -> str:
    """Best-effort decode when no session has a `cwd` field stored.

    The original encoding is lossy (slashes and hyphens collapse), so we
    just present the encoded form back unchanged when we have nothing better.
    """
    return encoded
