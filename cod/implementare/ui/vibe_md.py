"""
vibe-cli — VIBE.md learnings/preferences (global + project scope).

Two flat-markdown files give the agent a place to persist short, durable
learnings between sessions:

    ~/.vibe-cli/VIBE.md   global  → cross-project user preferences
    ./.vibe/VIBE.md       project → facts specific to this repo

Both are loaded into the system prompt every turn and edited by the agent
through the `remember` tool (or by hand). Files are capped (default 8 KB
each) to keep system-prompt overhead bounded; once the cap is hit the
tool refuses further writes and asks the model to consolidate.

This module is intentionally I/O-only — no parsing, no schema. Read,
render, atomically append.
"""

from __future__ import annotations

from pathlib import Path

from ui.paths import GLOBAL_VIBE_MD, project_vibe_dir, project_vibe_md
from ui.skills import _atomic_write


DEFAULT_MAX_BYTES = 8192


# ---------------------------------------------------------
# Reads
# ---------------------------------------------------------
def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def read_global_vibe() -> str:
    return _read(GLOBAL_VIBE_MD)


def read_project_vibe(cwd: Path | str | None = None) -> str:
    return _read(project_vibe_md(cwd))


# ---------------------------------------------------------
# Prompt block rendering
# ---------------------------------------------------------
def render_vibe_block(global_text: str, project_text: str) -> str:
    """Format the system-prompt block carrying VIBE.md contents.

    Empty inputs are skipped. Returns "" if both scopes are empty so the
    caller can omit the block entirely.
    """
    sections: list[str] = []
    if global_text:
        sections.append(
            "# Persistent preferences (~/.vibe-cli/VIBE.md)\n"
            "Apply these throughout the conversation — they capture how the "
            "user prefers to work across every project.\n\n"
            f"{global_text}"
        )
    if project_text:
        sections.append(
            "# Project notes (./.vibe/VIBE.md)\n"
            "Project-specific facts and conventions for THIS repo. Treat as "
            "ground truth alongside the codebase itself.\n\n"
            f"{project_text}"
        )
    return "\n\n".join(sections)


# ---------------------------------------------------------
# Writes (used by the `remember` tool)
# ---------------------------------------------------------
class VibeWriteError(Exception):
    """Raised when an append would exceed the per-file size cap, or the
    content is empty. The `remember` tool converts this into a model-facing
    error string."""


def _append(path: Path, content: str, *, max_bytes: int) -> int:
    """Atomically append `content` to `path` (creating it if absent).

    Returns the number of bytes written. Raises `VibeWriteError` if the
    resulting file would exceed `max_bytes`.
    """
    body = content.strip()
    if not body:
        raise VibeWriteError("content is empty")

    existing = _read(path)
    if existing:
        new_text = f"{existing}\n\n- {body}\n"
    else:
        new_text = f"- {body}\n"

    encoded = new_text.encode("utf-8")
    if len(encoded) > max_bytes:
        raise VibeWriteError(
            f"file would exceed cap ({len(encoded)} > {max_bytes} bytes); "
            "consolidate existing entries first"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, new_text)
    return len(body.encode("utf-8"))


def append_global(content: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> int:
    return _append(GLOBAL_VIBE_MD, content, max_bytes=max_bytes)


def append_project(
    content: str,
    *,
    cwd: Path | str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> int:
    # Ensure `.vibe/` exists at the project root before writing.
    project_vibe_dir(cwd).mkdir(parents=True, exist_ok=True)
    return _append(project_vibe_md(cwd), content, max_bytes=max_bytes)
