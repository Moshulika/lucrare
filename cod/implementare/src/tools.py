from __future__ import annotations
import contextvars
import subprocess
from pathlib import Path
from langchain_core.tools import InjectedToolCallId, tool
from typing_extensions import Annotated

_tool_ctx: contextvars.ContextVar["dict | None"] = contextvars.ContextVar(
    "vibe_tool_ctx", default=None)

def set_tool_ctx(ctx: dict | None):
    return _tool_ctx.set(ctx)

def reset_tool_ctx(token) -> None:
    _tool_ctx.reset(token)

def _ctx() -> dict:
    return _tool_ctx.get() or {}

@tool
def run_skill(skill_name: str) -> str:
    """Load the full instructions for a previously-listed skill.

    The system prompt lists available skills with name + description +
    when-to-use. Call this tool with a skill's `name` to retrieve its
    full instructions, then follow them for the rest of the turn.

    Args:
        skill_name: Name of an enabled skill (e.g. "pr-review").
    """
    import logging as _logging

    from src.logging_setup import emit_event as _emit, get_logger as _get_logger

    _log = _get_logger("skill")

    index = _ctx().get("skills_index")
    if index is None:
        _emit(
            _log,
            "skill.run",
            level=_logging.DEBUG,
            skill=skill_name,
            ok=False,
            reason="no_index",
        )
        return "No skills are loaded in this session."

    name = (skill_name or "").strip()
    if not name:
        _emit(
            _log,
            "skill.run",
            level=_logging.DEBUG,
            skill=name,
            ok=False,
            reason="missing_name",
        )
        return "Missing skill_name."

    skill = index.get(name) if hasattr(index, "get") else None
    if skill is None:
        _emit(
            _log,
            "skill.run",
            level=_logging.DEBUG,
            skill=name,
            ok=False,
            reason="not_found",
        )
        available = ", ".join(s.name for s in getattr(index, "active", []))
        return (
            f"Skill '{name}' not found. "
            f"Available enabled skills: {available or '(none)'}."
        )

    if not skill.enabled:
        _emit(
            _log,
            "skill.run",
            level=_logging.DEBUG,
            skill=name,
            ok=False,
            reason="disabled",
        )
        return f"Skill '{name}' is disabled. Enable it via /skills enable {name}."

    if not index.is_visible_to_model(name):
        _emit(
            _log,
            "skill.run",
            level=_logging.DEBUG,
            skill=name,
            ok=False,
            reason="dropped",
        )
        return (
            f"Skill '{name}' is enabled but over the active cap "
            f"({index.max_active}); it has been dropped from this session. "
            "Ask the user to raise `skills.max_active` or disable another skill."
        )

    if not skill.body:
        _emit(
            _log,
            "skill.run",
            level=_logging.DEBUG,
            skill=name,
            ok=False,
            reason="empty_body",
        )
        return f"Skill '{name}' has no instructions in its body."

    _emit(
        _log,
        "skill.run",
        level=_logging.DEBUG,
        skill=name,
        ok=True,
        body_bytes=len(skill.body),
    )
    return (
        f"=== Skill: {name} ===\n"
        "Apply the following instructions for the REST OF THIS TURN, then "
        "write your reply to the user's most recent message. Do not call "
        "another tool unless the instructions below tell you to.\n\n"
        f"{skill.body}\n\n"
        "=== End of skill ===\n"
        "Now produce your user-facing reply, applying the instructions above."
    )


@tool
def remember(scope: str, content: str) -> str:
    """Save a short, durable learning to VIBE.md so it persists across sessions.

    Use this when you learn something the next conversation should know:

    - scope="global" -> a user-wide preference (tone, naming style, "always do
      X", tools they prefer). Saved to ~/.vibe-cli/VIBE.md.
    - scope="project" -> a non-obvious fact about THIS repo (an architectural
      decision, owner, gotcha, convention). Saved to ./.vibe/VIBE.md.

    Do NOT use this for ephemeral task state, recent diffs, anything derivable
    from git/code, or secrets. Keep entries to one short paragraph or bullet.

    Args:
        scope: Either "global" or "project".
        content: One short paragraph or bullet. Be specific.
    """
    import logging as _logging

    from src.logging_setup import emit_event as _emit, get_logger as _get_logger
    from ui.vibe_md import (
        DEFAULT_MAX_BYTES,
        VibeWriteError,
        append_global,
        append_project,
    )

    _log = _get_logger("vibe_md")

    s = (scope or "").strip().lower()
    if s not in ("global", "project"):
        return (
            "Invalid scope. Use 'global' for user-wide preferences or "
            "'project' for facts about THIS repo."
        )

    body = (content or "").strip()
    if not body:
        return "Refusing to save empty content."

    ctx = _ctx()
    cap = int(
        (ctx.get("vibe_md_config") or {}).get("max_bytes_per_file") or DEFAULT_MAX_BYTES
    )

    try:
        if s == "global":
            written = append_global(body, max_bytes=cap)
            target = "~/.vibe-cli/VIBE.md"
        else:
            written = append_project(body, max_bytes=cap)
            target = "./.vibe/VIBE.md"
    except VibeWriteError as e:
        _emit(
            _log,
            "vibe_md.remember",
            level=_logging.WARNING,
            scope=s,
            ok=False,
            reason=str(e),
        )
        return (
            f"Could not save to {target}: {e}. "
            "Consolidate existing entries by hand, then try again."
        )

    _emit(_log, "vibe_md.remember", scope=s, ok=True, bytes=written)
    return f"Saved to {target} (+{written} bytes)."


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo. Returns a summary of the top results.

    Args:
        query: The search query.
        max_results: Maximum number of results to return (default 5).
    """
    try:
        from duckduckgo_search import DDGS

        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))

        if not results:
            return f"No results found for: {query}"

        lines = []
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. **{r['title']}**")
            lines.append(f"   {r['href']}")
            lines.append(f"   {r['body']}")
            lines.append("")
        return "\n".join(lines)

    except ImportError:
        return "Web search unavailable: install `duckduckgo-search` package."
    except Exception as e:
        return f"Search error: {e}"

@tool
def read_file(path: str, offset: int = 1, limit: int = 500) -> str:
    """Read a slice of a file by line range.

    Returns the requested lines prefixed with a header showing the slice and
    the file's total line count, e.g. `=== path (lines 1-500 of 1843) ===`.
    Use `offset` + `limit` to page through large files; widen `limit` only
    when you actually need more context - pulling thousands of lines wastes
    the context window.

    Args:
        path: Path to the file to read.
        offset: 1-indexed first line to return (default 1).
        limit: Maximum number of lines to return (default 500, max 2000).
    """
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"File not found: {path}"
        if not p.is_file():
            return f"Not a file: {path}"

        offset = max(1, int(offset))
        limit = max(1, min(int(limit), 2000))

        with p.open("r", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)

        if offset > total:
            return f"=== {path} (empty slice: offset {offset} > {total} lines) ==="

        end = min(offset + limit - 1, total)
        body = "".join(lines[offset - 1 : end])
        if body and not body.endswith("\n"):
            body += "\n"
        header = f"=== {path} (lines {offset}-{end} of {total}) ===\n"
        return header + body
    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Error reading file: {e}"


@tool
def write_file(path: str, content: str) -> str:
    """Write content to a file, replacing it entirely. Creates parent directories
    if needed.

    Use for **new files** or **complete rewrites**. For surgical changes to an
    existing file, prefer `edit_file` - rewriting a whole file just to change a
    few lines wastes tokens and risks dropping unrelated content.

    Args:
        path: Path to the file to write.
        content: The full content to write.
    """
    try:
        p = Path(path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"Written {len(content)} characters to {path}"
    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Error writing file: {e}"


@tool
def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace an exact string in a file with a new string.

    `old_string` must match **exactly once** in the file (including whitespace
    and indentation). If it matches zero or multiple times the edit is
    rejected - widen `old_string` with surrounding context to make it unique,
    then retry. Pass an empty `new_string` to delete the matched text.

    Prefer this over `write_file` whenever you're modifying an existing file:
    it's safer (the rest of the file can't drift) and far cheaper in tokens.

    Args:
        path: Path to the file to edit.
        old_string: Exact text to find. Must appear exactly once.
        new_string: Replacement text (empty string deletes).
    """
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"File not found: {path}"
        if not p.is_file():
            return f"Not a file: {path}"
        if old_string == new_string:
            return "Error: old_string and new_string are identical."
        if not old_string:
            return "Error: old_string is empty. Use write_file to create a new file."

        text = p.read_text(errors="replace")
        count = text.count(old_string)
        if count == 0:
            return (
                f"Error: old_string not found in {path}. "
                "Check whitespace/indentation, or use read_file to re-inspect."
            )
        if count > 1:
            return (
                f"Error: old_string matched {count} times in {path}. "
                "Add surrounding context to make it unique."
            )

        new_text = text.replace(old_string, new_string, 1)
        p.write_text(new_text)
        delta = len(new_text) - len(text)
        sign = "+" if delta >= 0 else ""
        return f"Edited {path} ({sign}{delta} chars)"
    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Error editing file: {e}"


@tool
def list_directory(path: str = ".", show_hidden: bool = False) -> str:
    """List files and directories at the given path.

    Args:
        path: Directory path to list (default: current directory).
        show_hidden: Whether to include hidden files (default: false).
    """
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"Directory not found: {path}"
        if not p.is_dir():
            return f"Not a directory: {path}"

        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        lines = [f"Contents of {p}:", ""]
        for entry in entries:
            if not show_hidden and entry.name.startswith("."):
                continue
            prefix = "📁 " if entry.is_dir() else "📄 "
            size = ""
            if entry.is_file():
                s = entry.stat().st_size
                if s < 1024:
                    size = f"  ({s}B)"
                elif s < 1_048_576:
                    size = f"  ({s / 1024:.1f}KB)"
                else:
                    size = f"  ({s / 1_048_576:.1f}MB)"
            lines.append(f"  {prefix}{entry.name}{size}")

        if len(lines) == 2:
            lines.append("  (empty)")
        return "\n".join(lines)
    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Error listing directory: {e}"

def _format_run_output(
    stdout: str,
    stderr: str,
    exit_code: int,
    timed_out: bool,
    timeout: int,
) -> str:
    if timed_out:
        return f"Command timed out after {timeout}s"
    parts: list[str] = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    if exit_code != 0:
        parts.append(f"[exit code: {exit_code}]")
    output = "\n".join(parts).strip()
    if len(output) > 10_000:
        output = output[:10_000] + "\n... (truncated)"
    return output if output else "(no output)"


def _run_on_host(command: str, timeout: int) -> str:
    try:
        r = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return _format_run_output(
            r.stdout or "",
            r.stderr or "",
            r.returncode,
            timed_out=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s"
    except Exception as e:
        return f"Error running command: {e}"


@tool
def run_command(command: str, timeout: int = 30) -> str:
    """Execute a shell command and return its output.

    When sandboxing is enabled, runs inside the per-session Docker container at
    /workspace (which is bind-mounted from your host cwd). Filesystem writes
    inside /workspace are visible on the host immediately. Writes outside
    /workspace are ephemeral.

    If you genuinely need host-only access (paths outside cwd, host-only
    binaries), use `run_command_host` instead - it requires explicit user
    approval per call.

    **Keep outputs small.** Output is truncated at 10k chars, which loses the
    tail. If you expect a large result, narrow it at the source rather than
    relying on truncation:
      - `grep -n PATTERN file` instead of dumping the file
      - `head -n 50` / `tail -n 50` to bound size
      - `wc -l file` to count before reading
      - `find ... | head` instead of unbounded `find`
      - `ls -la dir | head -n 30` for big directories
      - pipe through `awk` / `sed` / `jq` to extract just the fields you need

    For repetitive or one-off data work, drive Python from the shell:
    `python3 -c '...'` for short snippets, or `python3 path/to/script.py` for
    longer logic. Same truncation rules apply - print only what you need.

    Args:
        command: The shell command to execute.
        timeout: Maximum execution time in seconds (default 30).
    """
    sandbox = _ctx().get("sandbox")
    if sandbox is not None:
        result = sandbox.run(command, timeout=timeout)
        return _format_run_output(
            result.stdout,
            result.stderr,
            result.exit_code,
            timed_out=result.timed_out,
            timeout=timeout,
        )
    return _run_on_host(command, timeout)


@tool
def run_command_host(command: str, timeout: int = 30) -> str:
    """Execute a shell command on the HOST (outside the sandbox).

    Use only when you need access to files or tools that aren't available
    inside the sandbox container - for example, reading a sibling repo, or
    invoking a host-only binary. The user is prompted for permission per
    call; expect denials.

    Args:
        command: The shell command to execute on the host.
        timeout: Maximum execution time in seconds (default 30).
    """
    return _run_on_host(command, timeout)

@tool
async def spawn_subagent(
    subagent_type: str,
    prompt: str,
    timeout_s: int | None = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> str:
    """Spawn a specialised subagent to handle a focused sub-task in isolation.

    The subagent runs its own tool-calling loop with a filtered toolset and a
    dedicated system prompt, then returns a single final report. You only see
    that report - the subagent's intermediate steps are not surfaced. Use
    this to parallelise independent work or to delegate a deep dive without
    polluting your own context.

    Pick a `subagent_type` that matches the work:
      - `general`      - open-ended research / multi-step tasks (full toolset)
      - `code_search`  - read-only code lookups (paths, symbols, snippets)
      - `web_research` - web search + synthesis (no file/shell access)

    Call this tool multiple times in one assistant turn to spawn subagents
    in parallel - they run independently.

    Args:
        subagent_type: One of the registered types above.
        prompt: The task description for the subagent. Be specific: it has
            none of your conversation context.
        timeout_s: Optional hard timeout in seconds. Defaults to the type's
            built-in default; capped by config.
    """
    from src.subagents import run_subagent

    parent_ctx = _ctx()
    result = await run_subagent(
        subagent_type=(subagent_type or "").strip(),
        prompt=(prompt or "").strip(),
        parent_ctx=parent_ctx,
        timeout_s=timeout_s,
        parent_tool_call_id=tool_call_id or None,
    )
    if tool_call_id:
        parent_ctx.setdefault("pending_subagent_results", {})[tool_call_id] = {
            "subagent_type": (subagent_type or "").strip(),
            "prompt": (prompt or "").strip(),
            "text": result.text,
            "iterations": result.iterations,
            "tool_call_count": result.tool_call_count,
            "timed_out": result.timed_out,
            "error": result.error,
            "duration_s": result.duration_s,
            "usage": result.usage,
        }
    return result.text

_PDF_PAGE_RANGE_CAP = 20

@tool
def read_pdf_pages(
    name: str,
    start: int,
    end: int | None = None,
    as_image: bool = False,
) -> str:
    """Read specific pages from a previously-attached PDF.

    At attach time you got a *manifest* of the PDF (page count, TOC,
    short preview per page). Use it to pick a narrow range, then call
    this tool to read the content.

    Args:
        name: Filename of the PDF as it appeared in the manifest (e.g. "report.pdf").
        start: First page (1-indexed, inclusive).
        end: Last page (1-indexed, inclusive). Defaults to `start`.
            Max 20 pages per call regardless of `as_image`.
        as_image: When True, render each page to a PNG. The rendered
            images are delivered to you in the NEXT message - this tool
            call returns only a short confirmation, then a synthesized
            user-style message follows with the actual image blocks.
            Only works on image-capable models; refused otherwise.
            Use for figures, diagrams, tables, scanned content. Use
            `as_image=False` (default) for plain text fidelity, which
            is cheaper.
    """
    from src import attachments as _atts

    ctx = _ctx()
    registry = ctx.get("session_attachments") or {}
    att = registry.get(name)
    if att is None:
        for key, val in registry.items():
            if Path(key).name == Path(name).name:
                att = val
                break
    if att is None:
        attached = ", ".join(sorted(registry.keys())) or "(none)"
        return (
            f"Error: no PDF named {name!r} is attached to this session. "
            f"Currently attached: {attached}"
        )

    if getattr(att, "kind", None) != "pdf":
        return f"Error: attachment {name!r} is not a PDF (kind={att.kind})."

    page_count = getattr(att, "pdf_page_count", None) or 0
    if page_count <= 0:
        return f"Error: attachment {name!r} has no readable page count."

    if end is None:
        end = start
    if start < 1 or end < 1 or start > page_count or end > page_count or start > end:
        return (
            f"Error: page range [{start}, {end}] is invalid "
            f"({name!r} has {page_count} pages, 1-indexed)."
        )
    if end - start + 1 > _PDF_PAGE_RANGE_CAP:
        return (
            f"Error: range too wide ({end - start + 1} pages). "
            f"Limit is {_PDF_PAGE_RANGE_CAP} pages per call - narrow the range "
            "and call again."
        )

    session_id = ctx.get("session").id if ctx.get("session") else ""
    if not session_id:
        return "Error: no active session - cannot fetch PDF bytes."

    data = _atts.fetch_bytes(session_id, att.sha)
    if data is None:
        return f"Error: PDF bytes for {name!r} are no longer in the artifact store."

    if as_image:
        provider = ctx.get("provider") or ""
        model = ctx.get("model") or ""
        if not model:
            try:
                from src.main import provider_configs as _pc

                model = _pc.get(provider, {}).get("model", "") or ""
            except Exception:
                pass
        try:
            from src import capabilities as _caps

            if model and not _caps.get_capabilities(provider, model).supports("image"):
                return (
                    f"Error: as_image=True needs an image-capable model, but "
                    f"{model} is recorded as not supporting image input. "
                    "Use as_image=False to get text instead."
                )
        except Exception:
            pass

        try:
            shas = _render_pdf_pages_for_injection(
                data,
                start=start,
                end=end,
                session_id=session_id,
                dpi=int(
                    (ctx.get("multimodal_config") or {})
                    .get("pdf", {})
                    .get("render_dpi", 144)
                ),
            )
        except Exception as exc:
            return f"Error rendering PDF pages: {exc}"

        try:
            from src.multimodal import build_image_blocks_from_shas

            image_blocks = build_image_blocks_from_shas(
                shas, provider=provider, session_id=session_id, mime_type="image/png"
            )
        except Exception as exc:
            return f"Error encoding rendered pages: {exc}"

        ctx.setdefault("pending_image_injections", []).append(
            {
                "source": "read_pdf_pages",
                "pdf_name": att.name,
                "page_start": start,
                "page_end": end,
                "page_shas": shas,
                "image_blocks": image_blocks,
            }
        )
        return (
            f"Rendered pages {start}-{end} of {att.name} ({len(shas)} image"
            f"{'s' if len(shas) != 1 else ''}). Images delivered in the next "
            "message."
        )

    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        return (
            "Error: text extraction needs `pypdf` (base dep - refresh with "
            "`make setup` or `uv pip install pypdf`)."
        )

    import io

    reader = PdfReader(io.BytesIO(data))
    out: list[str] = [f"# {name} - pages {start}-{end} of {page_count}"]
    for i in range(start, end + 1):
        try:
            txt = reader.pages[i - 1].extract_text() or ""
        except Exception as exc:
            txt = f"(failed to extract: {exc})"
        out.append(f"\n--- page {i} ---\n{txt}")
    return "\n".join(out)


def _render_pdf_pages_for_injection(
    data: bytes,
    *,
    start: int,
    end: int,
    session_id: str,
    dpi: int,
) -> list[str]:
    try:
        import fitz 
    except ImportError as exc:
        raise RuntimeError(
            "PyMuPDF (`pymupdf`) is needed for as_image=True. "
            'Install with: uv pip install "vibe-cli[multimodal]"'
        ) from exc

    from src.artifacts import ArtifactStore

    store = ArtifactStore(session_id)

    doc = fitz.open(stream=data, filetype="pdf")
    try:
        shas: list[str] = []
        for i in range(start, end + 1):
            page = doc.load_page(i - 1)
            pix = page.get_pixmap(dpi=dpi)
            png = pix.tobytes("png")
            ref = store.put(png, kind="attachment.image", ext="png")
            shas.append(ref.sha256)
    finally:
        doc.close()
    return shas

ALL_TOOLS = [
    web_search,read_file,write_file,
    edit_file,list_directory,run_command,run_skill,
    remember,spawn_subagent,read_pdf_pages,
]
