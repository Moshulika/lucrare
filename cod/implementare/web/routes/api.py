from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path

from flask import Blueprint, Response, abort, jsonify, request, stream_with_context

from web.config import SESSIONS_DIR, VIBE_DIR
from web.data import repo
from web.data.metrics import compute_all
from web.data.sysinfo import snapshot

LOGS_DIR = VIBE_DIR / "logs"
PRIMARY_LOG = LOGS_DIR / "vibe-cli.log"
LIVE_WINDOW_S = 30.0

api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.get("/projects")
def projects():
    return jsonify(repo.list_projects())


@api_bp.get("/projects/<project_key>/sessions")
def sessions(project_key: str):
    return jsonify(repo.list_sessions(project_key))


@api_bp.get("/sessions/<project_key>/<session_id>")
def session_detail(project_key: str, session_id: str):
    data = repo.load_session(project_key, session_id)
    if data is None:
        abort(404)
    return jsonify(
        {
            "session": data,
            "metrics": compute_all(data),
        }
    )


@api_bp.get("/sessions/<project_key>/<session_id>/compaction")
def compaction(project_key: str, session_id: str):
    data = repo.load_session(project_key, session_id)
    if data is None:
        abort(404)
    snapshots = repo.list_pre_compaction_snapshots(project_key, session_id)
    metrics = compute_all(data)
    return jsonify(
        {
            "current": metrics["compaction"],
            "snapshots": snapshots,
            "diffs": _snapshot_diffs(snapshots, data),
        }
    )


@api_bp.get("/sysinfo")
def sysinfo():
    return jsonify(snapshot())


# ---------------------------------------------------------
# Tracing config (LangFuse link button).
# ---------------------------------------------------------
@api_bp.get("/tracing")
def tracing():
    """Return non-secret tracing config for the LangFuse link button.

    Uses the same preferences > config.yml > env resolution that
    `setup_tracing()` does, so credentials pasted via `/tracing setup`
    are picked up here too.
    """
    try:
        from src.tracing import resolve_tracing_settings
        settings = resolve_tracing_settings()
    except Exception:
        return jsonify({"enabled": False})

    enabled = bool(settings.get("enabled"))
    host = (settings.get("host") or "").rstrip("/")
    project = settings.get("project") or ""

    url = host if (enabled and host) else ""
    label = "LangFuse" if url else ""

    return jsonify({
        "enabled": enabled,
        "backend": "langfuse",
        "host": host,
        "project": project,
        "url": url,
        "label": label,
    })


# ---------------------------------------------------------
# Logs (latest text log file, last N lines).
# ---------------------------------------------------------
def _resolve_log_file() -> Path | None:
    if PRIMARY_LOG.exists():
        return PRIMARY_LOG
    if LOGS_DIR.exists():
        candidates = sorted(LOGS_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for c in candidates:
            if c.is_file():
                return c
    return None


def _log_info(path: Path | None) -> dict:
    if path is None:
        return {
            "available": False,
            "path": str(LOGS_DIR / "vibe-cli.log"),
            "filename": "vibe-cli.log",
            "size": 0,
            "mtime": 0.0,
            "is_live": False,
        }
    try:
        st = path.stat()
        return {
            "available": True,
            "path": str(path),
            "filename": path.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "is_live": (time.time() - st.st_mtime) < LIVE_WINDOW_S,
        }
    except OSError:
        return {"available": False, "path": str(path), "filename": path.name,
                "size": 0, "mtime": 0.0, "is_live": False}


def _tail_lines(path: Path, n: int) -> list[str]:
    """Return the last `n` lines of `path`. Capped at 5000."""
    n = max(1, min(n, 5000))
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            # Files are bounded by the rotating handler (~5MB). Simple is fine.
            return list(deque(f, maxlen=n))
    except OSError:
        return []


@api_bp.get("/logs/info")
def logs_info():
    return jsonify(_log_info(_resolve_log_file()))


@api_bp.get("/logs/tail")
def logs_tail():
    n = request.args.get("n", default=1000, type=int)
    path = _resolve_log_file()
    info = _log_info(path)
    info["lines"] = _tail_lines(path, n) if path is not None else []
    info["requested"] = n
    return jsonify(info)


# ---------------------------------------------------------
# Server-Sent Events: live refresh on session-file changes.
# ---------------------------------------------------------
_POLL_INTERVAL_S = 1.5
_KEEPALIVE_EVERY_S = 15.0


def _scan_state() -> dict[str, float]:
    """Map of every session file → its mtime (seconds)."""
    out: dict[str, float] = {}
    if not SESSIONS_DIR.exists():
        return out
    for project_dir in SESSIONS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for f in project_dir.glob("*.json"):
            try:
                out[f"{project_dir.name}/{f.stem}"] = f.stat().st_mtime
            except OSError:
                continue
    return out


def _diff(prev: dict[str, float], cur: dict[str, float]) -> list[dict]:
    changes: list[dict] = []
    for key, mt in cur.items():
        if key not in prev:
            changes.append({"key": key, "type": "added"})
        elif prev[key] != mt:
            changes.append({"key": key, "type": "modified"})
    for key in prev.keys() - cur.keys():
        changes.append({"key": key, "type": "removed"})
    return changes


def _log_signature() -> tuple[str, float, int]:
    """Stable signature of the current log file — changes on rotation/append."""
    p = _resolve_log_file()
    if p is None:
        return ("", 0.0, 0)
    try:
        st = p.stat()
        return (str(p), st.st_mtime, st.st_size)
    except OSError:
        return ("", 0.0, 0)


@api_bp.get("/events")
def events():
    """SSE stream — emits a `change` event for sessions and `logs_change` for the log file."""

    def gen():
        last = _scan_state()
        last_log = _log_signature()
        # Hello frame so the client knows the stream is live.
        yield "event: hello\ndata: {}\n\n"
        last_keepalive = time.monotonic()
        while True:
            time.sleep(_POLL_INTERVAL_S)
            cur = _scan_state()
            changes = _diff(last, cur)
            if changes:
                payload = json.dumps({"changes": changes})
                yield f"event: change\ndata: {payload}\n\n"
                last = cur
                last_keepalive = time.monotonic()
            cur_log = _log_signature()
            if cur_log != last_log:
                payload = json.dumps({
                    "path": cur_log[0],
                    "mtime": cur_log[1],
                    "size": cur_log[2],
                })
                yield f"event: logs_change\ndata: {payload}\n\n"
                last_log = cur_log
                last_keepalive = time.monotonic()
            if time.monotonic() - last_keepalive >= _KEEPALIVE_EVERY_S:
                yield ": keepalive\n\n"
                last_keepalive = time.monotonic()

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # disable proxy buffering if any
        "Connection": "keep-alive",
    }
    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers=headers,
    )


def _snapshot_diffs(snapshots, current):
    cur_events = len(current.get("events", []))
    cur_metrics = current.get("metrics", {}) or {}
    cur_input = (cur_metrics.get("tokens") or {}).get("input_total", 0)
    out = []
    for s in snapshots:
        sm = s.get("metrics", {}) or {}
        out.append(
            {
                "filename": s["filename"],
                "events_then": s["events"],
                "events_now": cur_events,
                "events_removed": max(0, s["events"] - cur_events),
                "input_then": (sm.get("tokens") or {}).get("input_total", 0),
                "input_now": cur_input,
                "input_freed": (sm.get("tokens") or {}).get("input_total", 0)
                - cur_input,
            }
        )
    return out