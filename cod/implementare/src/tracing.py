"""
vibe-cli — Observability / tracing setup.

LangFuse is the only supported backend; the historical `backend` field
in config.yml is ignored. Credentials can come from three places, listed
in precedence order:

  1. ~/.vibe-cli/preferences.json `tracing.{...}` (preferred — set via
     `/tracing setup`; never committed to git).
  2. config.yml `tracing.{...}` after `${ENV_VAR}` substitution.
  3. The bare LangFuse SDK env vars (`LANGFUSE_PUBLIC_KEY`,
     `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`).

Whatever is resolved is exported into the LangFuse env vars before the
v3 callback handler is constructed (the SDK reads from env at init).
"""

from __future__ import annotations

import os
import sys

from src.main import config
from ui import preferences

# Set after setup_tracing() actually constructs a LangFuse handler. The
# bottom toolbar reads this — config alone isn't enough, since the handler
# can fail to import or fail to authenticate.
_active_backend: str | None = None

# The last resolved view (preferences > config > env). Captured here so
# `/tracing` can show a live status string + masked keys without
# re-resolving.
_last_resolved: dict = {}

LANGFUSE_DEFAULT_HOST = "https://cloud.langfuse.com"


def resolve_tracing_settings() -> dict:
    """Merge preferences (highest priority) over config.yml (post-env).

    Returns a flat dict with the five known fields. Values may be empty
    strings — callers should treat falsy as "missing". Side-effect free.
    """
    cfg = config.get("tracing") or {}
    prefs = preferences.get_tracing()

    def pick(field: str, default: str = "") -> str:
        v = prefs.get(field)
        if v not in (None, ""):
            return str(v)
        v = cfg.get(field)
        if v not in (None, ""):
            return str(v)
        return default

    pref_enabled = prefs.get("enabled")
    cfg_enabled = bool(cfg.get("enabled", False))
    if pref_enabled is None:
        enabled = cfg_enabled
    else:
        # Preferences toggle wins, but config can't pretend to be
        # enabled if the file doesn't have a tracing block at all.
        enabled = bool(pref_enabled)

    return {
        "enabled": enabled,
        "project": pick("project"),
        "public_key": pick("public_key") or os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
        "secret_key": pick("secret_key") or os.environ.get("LANGFUSE_SECRET_KEY", ""),
        "host": pick("host") or os.environ.get("LANGFUSE_HOST", "")
        or LANGFUSE_DEFAULT_HOST,
    }


def setup_tracing() -> dict:
    """
    Initialize LangFuse tracing.

    Returns a dict of extra kwargs that the main loop merges into
    `graph.astream(..., **kwargs)` — `{"config": {"callbacks": [handler]}}`
    on success. Returns `{}` (no-op) when tracing is disabled,
    credentials are missing, or the SDK isn't installed.

    The `backend:` field in config.yml is ignored — LangFuse is the only
    supported backend. A one-time deprecation note is printed if it's
    set to anything other than `langfuse`.
    """
    cfg = config.get("tracing") or {}
    legacy_backend = (cfg.get("backend") or "").lower()
    if legacy_backend and legacy_backend != "langfuse":
        print(
            f"[tracing] ignoring legacy `backend: {legacy_backend}` — only "
            "LangFuse is supported. Remove the field from config.yml.",
            file=sys.stderr,
        )

    settings = resolve_tracing_settings()

    global _last_resolved
    _last_resolved = settings

    if not settings["enabled"]:
        return {}

    return _setup_langfuse(settings)


def _setup_langfuse(settings: dict) -> dict:
    """
    Build the LangFuse v3+ LangChain callback handler.

    The v3 SDK reads credentials from `LANGFUSE_PUBLIC_KEY` /
    `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` env vars at handler construction
    time, so we copy them out of `settings` first. The SDK also requires
    the umbrella `langchain` package — `langchain-core` alone isn't enough.
    """
    public_key = settings.get("public_key") or ""
    secret_key = settings.get("secret_key") or ""
    host = settings.get("host") or LANGFUSE_DEFAULT_HOST

    if not public_key or not secret_key:
        print(
            "[tracing] LangFuse public_key/secret_key missing — run "
            "`/tracing setup` to enter them, or export "
            "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY.",
            file=sys.stderr,
        )
        return {}

    os.environ["LANGFUSE_PUBLIC_KEY"] = public_key
    os.environ["LANGFUSE_SECRET_KEY"] = secret_key
    os.environ["LANGFUSE_HOST"] = host

    try:
        from langfuse.langchain import CallbackHandler as LangfuseHandler
    except ImportError as e:
        print(
            "[tracing] LangFuse LangChain integration failed to import. "
            "Install the tracing extras with "
            '`uv pip install "vibe-cli[tracing]"`. '
            f"Original error: {e}",
            file=sys.stderr,
        )
        return {}

    try:
        handler = LangfuseHandler()
    except Exception as e:
        print(f"[tracing] failed to construct LangFuse handler: {e}", file=sys.stderr)
        return {}

    global _active_backend
    _active_backend = "langfuse"
    return {"config": {"callbacks": [handler]}}


def get_tracing_info() -> str | None:
    """Return a short status string for the UI, or None if tracing is off.

    Only returns a string if `setup_tracing()` actually wired LangFuse in —
    config alone isn't enough, since the handler can fail to import or
    authenticate even with `enabled: true`.
    """
    if _active_backend is None:
        return None

    project = _last_resolved.get("project") or ""
    if project:
        return f"{_active_backend}:{project}"
    return _active_backend


def last_resolved_settings() -> dict:
    """Return the last-resolved tracing settings (for `/tracing` status)."""
    return dict(_last_resolved)


# ---------------------------------------------------------------------------
# LangFuse client helpers
# ---------------------------------------------------------------------------
# Used by the /score and /dataset slash commands. Each helper returns a
# (success, message) pair the caller can render directly — that keeps error
# handling out of the slash-command code paths and makes the helpers easy
# to unit-test by mocking `_get_client`.


def _get_client():
    """Return the LangFuse v3 singleton, or None if tracing isn't wired in.

    The singleton picks up the `LANGFUSE_*` env vars `_setup_langfuse`
    exported. We gate on `_active_backend` so callers don't accidentally
    hit a misconfigured client.
    """
    if _active_backend != "langfuse":
        return None
    try:
        from langfuse import get_client
    except ImportError:
        return None
    try:
        return get_client()
    except Exception:
        return None


def post_score(
    trace_id: str,
    name: str,
    value: float,
    comment: str | None = None,
) -> tuple[bool, str]:
    """Post a score to a LangFuse trace.

    Returns (success, message). The message is human-readable and ready
    to render in the chat (success or failure).
    """
    if not trace_id:
        return False, "no recent trace to score (send a message first)"

    client = _get_client()
    if client is None:
        return False, "tracing is off — enable LangFuse in config.yml"

    try:
        client.create_score(
            trace_id=trace_id,
            name=name,
            value=value,
            comment=comment,
        )
        # Force-flush so scores appear in the UI without waiting for
        # the SDK's background batch interval.
        try:
            client.flush()
        except Exception:
            pass
        return True, f"scored: {name}={value}" + (f" — {comment}" if comment else "")
    except Exception as e:
        return False, f"could not post score: {e}"


def add_dataset_item(
    dataset_name: str,
    input_payload: dict,
    expected_output: dict | None = None,
    metadata: dict | None = None,
) -> tuple[bool, str]:
    """Add an item to a LangFuse dataset (creating the dataset if missing)."""
    client = _get_client()
    if client is None:
        return False, "tracing is off — enable LangFuse in config.yml"

    try:
        # `create_dataset` is idempotent — calling it for an existing
        # dataset is a no-op rather than an error.
        client.create_dataset(name=dataset_name)
        client.create_dataset_item(
            dataset_name=dataset_name,
            input=input_payload,
            expected_output=expected_output,
            metadata=metadata or {},
        )
        try:
            client.flush()
        except Exception:
            pass
        return True, f"added to dataset: {dataset_name}"
    except Exception as e:
        return False, f"could not add to dataset: {e}"


def list_datasets() -> tuple[bool, list[dict] | str]:
    """List datasets in the current LangFuse project.

    Returns (True, [{name, item_count, description}, ...]) on success,
    or (False, error_message) on failure.
    """
    client = _get_client()
    if client is None:
        return False, "tracing is off — enable LangFuse in config.yml"

    try:
        page = client.api.datasets.list()
    except Exception as e:
        return False, f"could not list datasets: {e}"

    items = []
    for ds in getattr(page, "data", []) or []:
        items.append(
            {
                "name": getattr(ds, "name", ""),
                "description": getattr(ds, "description", "") or "",
                "item_count": getattr(ds, "item_count", None),
            }
        )
    return True, items
