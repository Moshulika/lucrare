from __future__ import annotations

from pathlib import Path

from ui.paths import (
    ARTIFACTS_DIR,
    SESSION_BACKUPS_DIR,
    SESSIONS_DIR,
    VIBE_DIR,
)

HOST = "127.0.0.1"
PORT = 5050

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

__all__ = [
    "HOST",
    "PORT",
    "WEB_DIR",
    "TEMPLATES_DIR",
    "STATIC_DIR",
    "VIBE_DIR",
    "SESSIONS_DIR",
    "SESSION_BACKUPS_DIR",
    "ARTIFACTS_DIR",
]