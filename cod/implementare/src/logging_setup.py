"""
vibe-cli — Stdlib logging setup, JSON envelope, redaction, retention.

Two streams over the same records:

* ``vibe-cli.log``      human-readable rotating text (``RotatingFileHandler``)
* ``events/<date>.jsonl`` machine-readable daily JSON-lines, gzipped on rollover

The JSON envelope (one record = one event) is::

    {
      "ts", "level", "category", "event",
      "session_id?", "turn_id?", "event_id?",
      "pid", "logger",
      "duration_ms?", "data?": { ... },
      "exc_info?": "..."
    }

Logger naming convention: ``vibe.<category>`` (e.g. ``vibe.tool_call``,
``vibe.llm``, ``vibe.command``). The ``category`` field on the record is
derived from the logger name; the ``event`` field comes from
``logger.info("foo.bar", extra={"event": "foo.bar", ...})``.

Redaction is centralised in :func:`redact` — see its docstring.
"""

from __future__ import annotations

import gzip
import json
import logging
import logging.handlers
import os
import platform
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.log_context import get_correlation

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_LOG_DIR = Path.home() / ".vibe-cli" / "logs"
DEFAULT_LEVEL = "INFO"
DEFAULT_TEXT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
DEFAULT_TEXT_BACKUPS = 10
DEFAULT_JSONL_RETENTION_DAYS = 30

# Substring patterns whose values we redact wholesale.
_SECRET_KEY_PATTERNS = (
    "api_key", "apikey", "secret", "token", "authorization", "auth_header",
    "password", "passwd", "session_token", "bearer",
)
_REDACTED = "***REDACTED***"
_MAX_STR_LEN = 200


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
def _looks_secret(key: str) -> bool:
    k = key.lower()
    return any(pat in k for pat in _SECRET_KEY_PATTERNS)


def _shorten_path(s: str) -> str:
    home = str(Path.home())
    if home and s.startswith(home):
        return "~" + s[len(home):]
    return s


def _truncate(s: str, limit: int = _MAX_STR_LEN) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + "…"


def redact(value: Any, _depth: int = 0) -> Any:
    """Return a log-safe copy of ``value``.

    Rules:
      * dict keys matching secret patterns → ``***REDACTED***``
      * absolute home paths shortened to ``~/...``
      * strings truncated to 200 chars
      * objects we can't serialise are repr()'d and truncated
      * recursion bounded at 6 levels
    """
    if _depth > 6:
        return _truncate(repr(value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate(_shorten_path(value))
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            ks = str(k)
            if _looks_secret(ks):
                out[ks] = _REDACTED
            else:
                out[ks] = redact(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [redact(v, _depth + 1) for v in list(value)[:50]]
    if isinstance(value, Path):
        return _truncate(_shorten_path(str(value)))
    return _truncate(repr(value))


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------
def _category_from_logger(name: str) -> str:
    if name.startswith("vibe."):
        return name[len("vibe."):]
    return name


def _iso_ts(created: float) -> str:
    return (
        datetime.fromtimestamp(created, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the vibe-cli envelope."""

    def format(self, record: logging.LogRecord) -> str:
        envelope: dict[str, Any] = {
            "ts": _iso_ts(record.created),
            "level": record.levelname,
            "category": _category_from_logger(record.name),
            "event": getattr(record, "event", None) or record.getMessage(),
            "logger": record.name,
            "pid": record.process,
        }
        envelope.update(get_correlation())

        duration = getattr(record, "duration_ms", None)
        if duration is not None:
            envelope["duration_ms"] = duration

        # Caller convention: ``extra={"event": "...", "data": {...}}``.
        # `data` is the single safe key; anything else collides with reserved
        # LogRecord attrs (name, msg, module, ...).
        data = getattr(record, "data", None)
        if data is not None:
            envelope["data"] = redact(data)

        if record.exc_info:
            envelope["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(envelope, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Compact human-readable line for tail -f."""

    def format(self, record: logging.LogRecord) -> str:
        ts = _iso_ts(record.created)
        cat = _category_from_logger(record.name)
        event = getattr(record, "event", None) or record.getMessage()
        corr = get_correlation()
        sid = corr.get("session_id", "")
        tid = corr.get("turn_id", "")
        sid_short = sid[:8] if sid else "-"
        tid_short = tid[:8] if tid else "-"
        line = (
            f"{ts} {record.levelname:<5} {cat:<12} "
            f"[{sid_short}/{tid_short}] {event}"
        )
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def _gzip_namer(default_name: str) -> str:
    return default_name + ".gz"


def _gzip_rotator(source: str, dest: str) -> None:
    with open(source, "rb") as src, gzip.open(dest, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.remove(source)


class _DailyJsonlHandler(logging.Handler):
    """Daily-rotated JSONL file. Each UTC day has its own ``YYYY-MM-DD.jsonl``.

    On the first emit of a new day, the previous day's file is gzipped to
    ``YYYY-MM-DD.jsonl.gz`` (best-effort — failures keep the plaintext file).
    Retention is handled separately by :func:`sweep_old_logs`.
    """

    def __init__(self, log_dir: Path) -> None:
        super().__init__()
        self._events_dir = log_dir / "events"
        self._events_dir.mkdir(parents=True, exist_ok=True)
        self._current_date: str | None = None
        self._stream = None

    def _date_str(self) -> str:
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    def _open_for(self, date_str: str):
        path = self._events_dir / f"{date_str}.jsonl"
        return open(path, "a", encoding="utf-8")

    def _maybe_gzip(self, date_str: str) -> None:
        path = self._events_dir / f"{date_str}.jsonl"
        gz_path = self._events_dir / f"{date_str}.jsonl.gz"
        if not path.exists() or gz_path.exists():
            return
        try:
            _gzip_rotator(str(path), str(gz_path))
        except Exception:
            pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            today = self._date_str()
            if today != self._current_date:
                if self._stream is not None:
                    try:
                        self._stream.close()
                    except Exception:
                        pass
                    self._stream = None
                if self._current_date is not None:
                    self._maybe_gzip(self._current_date)
                self._stream = self._open_for(today)
                self._current_date = today
            line = self.format(record)
            self._stream.write(line + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        finally:
            super().close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_configured = False


def configure_logging(
    *,
    level: str | int | None = None,
    log_dir: Path | str | None = None,
    text_max_bytes: int = DEFAULT_TEXT_MAX_BYTES,
    text_backups: int = DEFAULT_TEXT_BACKUPS,
    jsonl_retention_days: int = DEFAULT_JSONL_RETENTION_DAYS,
    enabled: bool = True,
    force: bool = False,
) -> Path | None:
    """Configure the ``vibe`` logger tree. Returns the resolved log dir.

    Idempotent unless ``force=True``. Honors ``VIBE_LOG_LEVEL`` env override.
    Returns ``None`` when ``enabled=False``.
    """
    global _configured
    if _configured and not force:
        return Path(log_dir) if log_dir else DEFAULT_LOG_DIR
    if not enabled:
        # Still install a NullHandler so ``logging.getLogger("vibe")`` doesn't
        # complain about no handlers.
        root = logging.getLogger("vibe")
        root.handlers.clear()
        root.addHandler(logging.NullHandler())
        root.propagate = False
        _configured = True
        return None

    resolved_dir = Path(log_dir).expanduser() if log_dir else DEFAULT_LOG_DIR
    resolved_dir.mkdir(parents=True, exist_ok=True)

    env_level = os.environ.get("VIBE_LOG_LEVEL")
    raw_level = env_level or level or DEFAULT_LEVEL
    if isinstance(raw_level, str):
        eff_level_int = logging.getLevelName(raw_level.upper())
        if not isinstance(eff_level_int, int):
            eff_level_int = logging.INFO
    else:
        eff_level_int = int(raw_level)

    root = logging.getLogger("vibe")
    root.handlers.clear()
    root.setLevel(eff_level_int)
    root.propagate = False

    # Text — for `tail -f`. INFO+ only; the JSONL is the firehose.
    text_handler = logging.handlers.RotatingFileHandler(
        filename=str(resolved_dir / "vibe-cli.log"),
        maxBytes=text_max_bytes,
        backupCount=text_backups,
        encoding="utf-8",
        delay=True,
    )
    text_handler.setLevel(max(eff_level_int, logging.INFO))
    text_handler.setFormatter(TextFormatter())
    root.addHandler(text_handler)

    # JSONL — full firehose at the configured level.
    json_handler = _DailyJsonlHandler(resolved_dir)
    json_handler.setLevel(eff_level_int)
    json_handler.setFormatter(JsonFormatter())
    root.addHandler(json_handler)

    _configured = True

    # Sweep old JSONL files (best-effort, never raises).
    try:
        sweep_old_logs(resolved_dir, retention_days=jsonl_retention_days)
    except Exception:
        pass

    return resolved_dir


def sweep_old_logs(
    log_dir: Path,
    *,
    retention_days: int = DEFAULT_JSONL_RETENTION_DAYS,
) -> int:
    """Delete events/*.jsonl(.gz) older than retention_days. Returns count."""
    if retention_days <= 0:
        return 0
    cutoff = time.time() - retention_days * 86400
    events_dir = log_dir / "events"
    if not events_dir.is_dir():
        return 0
    removed = 0
    for p in events_dir.iterdir():
        if not p.is_file():
            continue
        suf = "".join(p.suffixes)
        if not (suf.endswith(".jsonl") or suf.endswith(".jsonl.gz")):
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def get_logger(category: str) -> logging.Logger:
    """Return a ``vibe.<category>`` logger."""
    return logging.getLogger(f"vibe.{category}")


def emit_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    duration_ms: float | None = None,
    exc_info=None,
    **data: Any,
) -> None:
    """Emit one structured event. Keyword args become the ``data`` payload.

    ``logger.info("foo", extra={"event": "foo", "name": "x"})`` collides with
    ``LogRecord.name``; this helper packs the payload into the single safe
    ``data`` key.
    """
    extra: dict[str, Any] = {"event": event}
    if data:
        extra["data"] = data
    if duration_ms is not None:
        extra["duration_ms"] = duration_ms
    logger.log(level, event, extra=extra, exc_info=exc_info)


def log_app_environment() -> dict:
    """Build the metadata block for the ``app.start`` event."""
    try:
        from importlib.metadata import version

        try:
            ver = version("vibe-cli")
        except Exception:
            ver = "unknown"
    except Exception:
        ver = "unknown"
    return {
        "version": ver,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "argv": sys.argv,
    }