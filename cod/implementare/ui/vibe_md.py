from __future__ import annotations
from pathlib import Path
from ui.paths import GLOBAL_VIBE_MD, project_vibe_dir, project_vibe_md
from ui.skills import _atomic_write

DEFAULT_MAX_BYTES = 8192

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""

def read_global_vibe() -> str:
    return _read(GLOBAL_VIBE_MD)

def read_project_vibe(cwd: Path | str | None = None) -> str:
    return _read(project_vibe_md(cwd))

def render_vibe_block(global_text: str, project_text: str) -> str:
    sections: list[str] = []
    if global_text:
        sections.append(
            "# Persistent preferences (~/.vibe-cli/VIBE.md)\n"
            "Apply these throughout the conversation - they capture how the "
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

class VibeWriteError(Exception):
    ...

def _append(path: Path, content: str, *, max_bytes: int) -> int:
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
    project_vibe_dir(cwd).mkdir(parents=True, exist_ok=True)
    return _append(project_vibe_md(cwd), content, max_bytes=max_bytes)
