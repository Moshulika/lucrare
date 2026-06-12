"""
vibe-cli — local user profile.

A single UUID identifying this install, persisted at
``~/.vibe-cli/profile.json``. Used as the LangFuse ``user_id`` so traces
from the same machine group together in the LangFuse UI.

The profile is intentionally minimal: just an ID. It is created on
first read and never contains PII unless the user explicitly adds it.
The file is local-only — vibe-cli never sends the contents anywhere
except as a ``user_id`` string when LangFuse tracing is enabled.
"""

from __future__ import annotations

import json
import uuid

from .paths import VIBE_DIR

PROFILE_PATH = VIBE_DIR / "profile.json"

# Prefix so the ID self-identifies in the LangFuse UI when a project is
# shared across multiple tools / tenants.
_USER_ID_PREFIX = "vibe-"


def get_or_create_profile_id() -> str:
    """Return the persisted ``user_id``, creating ``profile.json`` if missing.

    Recovers gracefully from a malformed/truncated file by overwriting
    it with a fresh ID — the profile is not load-bearing state, so a
    bad file should not block the CLI.
    """
    try:
        data = json.loads(PROFILE_PATH.read_text())
        uid = data.get("user_id")
        if isinstance(uid, str) and uid:
            return uid
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    uid = f"{_USER_ID_PREFIX}{uuid.uuid4()}"
    VIBE_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps({"user_id": uid}, indent=2) + "\n")
    return uid
