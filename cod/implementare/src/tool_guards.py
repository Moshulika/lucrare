from __future__ import annotations
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

Precondition = Callable[[dict, dict], str | None]
"""Signature: (args, ctx) -> error message or None.

Return None to allow the tool to run. Return a string to short-circuit the
call; the string becomes the tool's `ToolMessage.content`."""

PostHook = Callable[[dict, str, dict], None]
"""Signature: (args, result, ctx) -> None.

Runs after a successful tool invocation to update tracked state (e.g.
remember a file's SHA after read_file)."""

PRECONDITIONS: dict[str, list[tuple[str, Precondition]]] = {}
POST_HOOKS: dict[str, list[tuple[str, PostHook]]] = {}

def precondition(tool_name: str, *, guard_name: str):
    """Register `fn` as a precondition for `tool_name`.

    `guard_name` identifies the rule in logs / web UI tooltips so users can
    tell why a tool was rejected without parsing the error string.
    """

    def deco(fn: Precondition) -> Precondition:
        PRECONDITIONS.setdefault(tool_name, []).append((guard_name, fn))
        return fn

    return deco

def post_hook(tool_name: str, *, name: str):
    """Register `fn` to run after a successful invocation of `tool_name`.

    Used to update state that other guards read (e.g. recording a file's
    SHA after read_file).
    """

    def deco(fn: PostHook) -> PostHook:
        POST_HOOKS.setdefault(tool_name, []).append((name, fn))
        return fn

    return deco

def check_preconditions(
    tool_name: str, args: dict, ctx: dict
) -> tuple[str, str] | None:
    """Run all preconditions for `tool_name` in registration order.

    Returns `(guard_name, error_message)` for the first guard that denies,
    or None if all allow. Guards are skipped entirely when
    `ctx.tool_guards.enabled` is False (master kill switch).
    """
    cfg = _config(ctx)
    if not cfg.get("enabled", True):
        return None
    for guard_name, fn in PRECONDITIONS.get(tool_name, []):
        try:
            err = fn(args, ctx)
        except Exception as exc:  # pragma: no cover - guards should not throw
            err = f"[tool-guard '{guard_name}' raised {type(exc).__name__}: {exc}]"
        if err:
            return (guard_name, err)
    return None

def run_post_hooks(tool_name: str, args: dict, result: str, ctx: dict) -> None:
    """Run all post-hooks for `tool_name`. Best-effort - exceptions swallowed."""
    cfg = _config(ctx)
    if not cfg.get("enabled", True):
        return
    for _name, fn in POST_HOOKS.get(tool_name, []):
        try:
            fn(args, result, ctx)
        except Exception:  
            pass

def _config(ctx: dict) -> dict:
    """Return the tool_guards config block (always a dict; empty by default)."""
    cfg = ctx.get("tool_guards_config")
    return cfg if isinstance(cfg, dict) else {}

def _guard_config(ctx: dict, guard_name: str) -> dict:
    """Per-guard config block under `tool_guards.<guard_name>`."""
    raw = _config(ctx).get(guard_name)
    return raw if isinstance(raw, dict) else {}

def _resolved_path(raw: Any) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except Exception:
        return None

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

def _file_reads(ctx: dict) -> dict[str, str]:
    """Get-or-create the per-session read tracker."""
    fr = ctx.get("file_reads")
    if not isinstance(fr, dict):
        fr = {}
        ctx["file_reads"] = fr
    return fr

@post_hook("read_file", name="track_read")
def _track_read(args: dict, result: str, ctx: dict) -> None:
    """Remember the SHA of files we've read so write/edit can verify freshness.
    """
    p = _resolved_path(args.get("path"))
    if p is None or not p.exists() or not p.is_file():
        return
    try:
        content = p.read_text(errors="replace")
    except Exception:
        return
    _file_reads(ctx)[str(p)] = _sha(content)


@post_hook("write_file", name="track_write")
@post_hook("edit_file", name="track_edit")
def _track_mutation(args: dict, result: str, ctx: dict) -> None:
    """After a successful write/edit, refresh the tracked SHA so subsequent
    writes within the same session don't trip the freshness check on the
    very content we just produced."""
    if not isinstance(result, str):
        return
    if not (result.startswith("Written ") or result.startswith("Edited ")):
        return
    p = _resolved_path(args.get("path"))
    if p is None or not p.exists() or not p.is_file():
        return
    try:
        content = p.read_text(errors="replace")
    except Exception:
        return
    _file_reads(ctx)[str(p)] = _sha(content)


def _require_read_before_mutate(
    args: dict, ctx: dict, *, action: str
) -> str | None:
    guard_cfg = _guard_config(ctx, "read_before_write")
    if guard_cfg.get("enabled") is False:
        return None
    p = _resolved_path(args.get("path"))
    if p is None or not p.exists() or not p.is_file():
        return None  # creating a new file - no read needed.

    tracked_sha = _file_reads(ctx).get(str(p))
    if tracked_sha is None:
        return (
            f"Refusing to {action} '{args.get('path')}': you haven't read this "
            f"file in the current session. Call read_file({{'path': "
            f"'{args.get('path')}'}}) first so you know what you're overwriting, "
            f"then retry the {action}."
        )

    if guard_cfg.get("check_freshness", True):
        try:
            current = p.read_text(errors="replace")
        except Exception:
            return None 
        if _sha(current) != tracked_sha:
            return (
                f"Refusing to {action} '{args.get('path')}': the file has "
                f"changed on disk since you last read it (external edit or a "
                f"prior write you didn't track). Call read_file({{'path': "
                f"'{args.get('path')}'}}) again to see the current content, "
                f"then retry."
            )
    return None

@precondition("write_file", guard_name="read_before_write")
def _write_requires_read(args: dict, ctx: dict) -> str | None:
    return _require_read_before_mutate(args, ctx, action="write")

@precondition("edit_file", guard_name="read_before_write")
def _edit_requires_read(args: dict, ctx: dict) -> str | None:
    return _require_read_before_mutate(args, ctx, action="edit")

__all__ = [
    "PRECONDITIONS","POST_HOOKS","precondition",
    "post_hook","check_preconditions","run_post_hooks",
]
