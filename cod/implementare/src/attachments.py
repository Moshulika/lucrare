"""
vibe-cli — User attachment handling for multimodal turns.

Attachments are files (images, PDFs, plain text / code) the user wants to send
to the model in the next turn. Three lifecycle phases:

  1. **Pending** — added by `/attach <path>` (or paste interception). Lives
     on `ctx["pending_attachments"]` until the next turn submits.
  2. **In-flight** — at submit time `build_human_content` (see
     `src/multimodal.py`) expands them into provider-native content blocks.
  3. **Session-scoped** — once submitted, they also land on
     `ctx["session_attachments"][name]` so cross-turn tools (notably
     `read_pdf_pages`) can look them up by name.

Bytes spill into the existing content-addressed artifact store
(`src/artifacts.py`) keyed by sha256 — the session event JSON only carries
the sha + metadata, never the raw bytes.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re as _re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from src.artifacts import ArtifactStore

logger = logging.getLogger(__name__)

Kind = Literal["image", "pdf", "text"]

# Per-attachment + per-turn byte caps. Anything above is refused with a
# friendly message rather than silently truncated.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_TURN_TOTAL_BYTES = 20 * 1024 * 1024  # 20 MB

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
PDF_EXTS = {".pdf"}
TEXT_EXTS = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".log",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    # Common code extensions:
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".sh",
    ".bash",
    ".zsh",
    ".rb",
    ".php",
    ".kt",
    ".swift",
    ".sql",
    ".html",
    ".css",
    ".xml",
}


class AttachmentError(Exception):
    """Raised when an attachment cannot be loaded (missing, too big, etc.)."""


@dataclass
class Attachment:
    """A user-attached file, materialised in the artifact store."""

    path: str  # absolute source path the user gave us
    name: str  # display name (basename of path)
    kind: Kind
    mime_type: str
    size: int
    sha: str
    ext: str  # extension without leading dot
    text_content: str | None = None  # populated for `kind == "text"`
    pdf_manifest: str | None = None  # populated for `kind == "pdf"`
    pdf_page_count: int | None = None
    # PDF rendering plan, set at load time. "full" → pages were rendered
    # eagerly to PNGs (one per page) and live in `rendered_page_shas`,
    # ready to inline in the HumanMessage. "manifest" → only the text
    # manifest goes inline; model fetches pages via read_pdf_pages.
    pdf_render_strategy: str = "manifest"  # "full" | "manifest"
    rendered_page_shas: list[str] = field(default_factory=list)
    # Cached full-text extract for PDF "full mode" so we don't re-decode
    # bytes from the artifact store on each turn.
    pdf_full_text: str | None = None
    # Extra metadata kept for the event-log payload — serialise via to_block().
    extra: dict = field(default_factory=dict)

    def to_block(self) -> dict:
        """Serialisable form stored on the human session event."""
        out: dict = {
            "type": "attachment",
            "kind": self.kind,
            "name": self.name,
            "media_type": self.mime_type,
            "sha": self.sha,
            "ext": self.ext,
            "size": self.size,
        }
        if self.kind == "text" and self.text_content is not None:
            # Keep text inline in the event so we don't depend on the artifact
            # store for re-hydration on resume. Texts are small by definition
            # (capped at MAX_ATTACHMENT_BYTES = 10MB but typically <50KB).
            out["text"] = self.text_content
        if self.kind == "pdf":
            if self.pdf_manifest is not None:
                out["pdf_manifest"] = self.pdf_manifest
            if self.pdf_page_count is not None:
                out["pdf_page_count"] = self.pdf_page_count
            if self.pdf_render_strategy != "manifest":
                out["pdf_render_strategy"] = self.pdf_render_strategy
            if self.rendered_page_shas:
                out["rendered_page_shas"] = list(self.rendered_page_shas)
            if self.pdf_full_text is not None:
                out["pdf_full_text"] = self.pdf_full_text
        return out

    @classmethod
    def from_block(cls, block: dict) -> "Attachment":
        """Reconstruct from a stored block (the inverse of to_block)."""
        return cls(
            path="",  # source path not retained after submission
            name=block.get("name", ""),
            kind=block.get("kind", "text"),  # type: ignore[arg-type]
            mime_type=block.get("media_type", "application/octet-stream"),
            size=int(block.get("size", 0)),
            sha=block.get("sha", ""),
            ext=block.get("ext", ""),
            text_content=block.get("text"),
            pdf_manifest=block.get("pdf_manifest"),
            pdf_page_count=block.get("pdf_page_count"),
            pdf_render_strategy=block.get("pdf_render_strategy", "manifest"),
            rendered_page_shas=list(block.get("rendered_page_shas", []) or []),
            pdf_full_text=block.get("pdf_full_text"),
        )


# ---------------------------------------------------------
# Detection
# ---------------------------------------------------------
def detect_kind(path: Path | str) -> Kind | None:
    """Return "image" / "pdf" / "text", or None if the type isn't supported.

    Detection order: extension match → mimetype guess. Doesn't read the file.
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in TEXT_EXTS:
        return "text"
    # Mimetype fallback for unusual extensions.
    mt, _ = mimetypes.guess_type(str(p))
    if not mt:
        return None
    if mt.startswith("image/"):
        return "image"
    if mt == "application/pdf":
        return "pdf"
    if mt.startswith("text/"):
        return "text"
    return None


def mime_for(path: Path, kind: Kind) -> str:
    """Best-effort MIME type for an attachment."""
    mt, _ = mimetypes.guess_type(str(path))
    if mt:
        return mt
    if kind == "image":
        return f"image/{path.suffix.lstrip('.').lower() or 'png'}"
    if kind == "pdf":
        return "application/pdf"
    return "text/plain"


# ---------------------------------------------------------
# Loading
# ---------------------------------------------------------
def load_attachment(
    path: Path | str,
    *,
    session_id: str,
    image_capable: bool = True,
    small_threshold_pages: int = 15,
    render_dpi: int = 144,
) -> Attachment:
    """Validate + load a file into the per-session artifact store.

    For PDFs, decides between "full" mode (≤ small_threshold_pages AND
    image_capable AND pymupdf available) and "manifest" mode. Full mode
    eagerly renders each page to PNG and stores them as artifacts. Manifest
    mode keeps the lightweight TOC + previews flow.

    Raises `AttachmentError` on invalid input (missing file, oversize, etc.).
    Rendering / parsing failures degrade gracefully to manifest mode with
    a logged note — they never block the attach.
    """
    p = Path(path).expanduser()
    if not p.exists():
        raise AttachmentError(f"file not found: {p}")
    if not p.is_file():
        raise AttachmentError(f"not a regular file: {p}")
    kind = detect_kind(p)
    if kind is None:
        raise AttachmentError(
            f"unsupported file type: {p.name} "
            "(supported: PNG/JPG/JPEG/WebP/GIF, PDF, common text/code files)"
        )

    size = p.stat().st_size
    if size > MAX_ATTACHMENT_BYTES:
        mb = MAX_ATTACHMENT_BYTES // (1024 * 1024)
        raise AttachmentError(
            f"{p.name} is {size / (1024 * 1024):.1f} MB; per-file cap is {mb} MB"
        )
    if size == 0:
        raise AttachmentError(f"{p.name} is empty")

    data = p.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    ext = p.suffix.lstrip(".").lower() or "bin"
    mt = mime_for(p, kind)

    # Spill the bytes to the session's artifact store. Idempotent on sha.
    store = ArtifactStore(session_id)
    store.put(data, kind=f"attachment.{kind}", ext=ext)

    text_content: str | None = None
    pdf_manifest: str | None = None
    pdf_page_count: int | None = None

    if kind == "text":
        try:
            text_content = data.decode("utf-8")
        except UnicodeDecodeError:
            # Best-effort decode — show what we can, but warn.
            text_content = data.decode("utf-8", errors="replace")
            logger.info("attachment.text_decode_replace path=%s", p)
    elif kind == "pdf":
        try:
            pdf_manifest, pdf_page_count = _build_pdf_manifest(data, name=p.name)
        except _PdfMissingError as exc:
            raise AttachmentError(str(exc)) from exc
        except Exception as exc:  # pypdf-side failure
            raise AttachmentError(f"failed to parse {p.name}: {exc}") from exc

    # PDF tier decision: full mode (eager render) vs manifest mode.
    pdf_render_strategy = "manifest"
    rendered_page_shas: list[str] = []
    pdf_full_text: str | None = None
    if (
        kind == "pdf"
        and pdf_page_count is not None
        and pdf_page_count <= small_threshold_pages
        and image_capable
        and _pymupdf_available()
    ):
        try:
            rendered_page_shas = _render_pdf_pages_to_store(
                data, session_id=session_id, dpi=render_dpi
            )
            pdf_full_text = _extract_pdf_full_text(data)
            pdf_render_strategy = "full"
        except Exception as exc:
            # Rendering failure is non-fatal — fall back to manifest mode
            # so the attach still works.
            logger.info(
                "attachment.pdf_render_failed path=%s error=%s — falling back to manifest mode",
                p,
                exc,
            )
            rendered_page_shas = []
            pdf_full_text = None
            pdf_render_strategy = "manifest"

    return Attachment(
        path=str(p),
        name=p.name,
        kind=kind,
        mime_type=mt,
        size=size,
        sha=sha,
        ext=ext,
        text_content=text_content,
        pdf_manifest=pdf_manifest,
        pdf_page_count=pdf_page_count,
        pdf_render_strategy=pdf_render_strategy,
        rendered_page_shas=rendered_page_shas,
        pdf_full_text=pdf_full_text,
    )


def total_size(attachments: list[Attachment]) -> int:
    return sum(a.size for a in attachments)


def check_turn_budget(
    new_size: int, pending: list[Attachment]
) -> None:
    """Raise AttachmentError if adding `new_size` would exceed the per-turn cap."""
    if total_size(pending) + new_size > MAX_TURN_TOTAL_BYTES:
        mb = MAX_TURN_TOTAL_BYTES // (1024 * 1024)
        raise AttachmentError(
            f"total attachments would exceed the per-turn cap of {mb} MB"
        )


# ---------------------------------------------------------
# PDF manifest
# ---------------------------------------------------------
class _PdfMissingError(Exception):
    pass


_PDF_PREVIEW_CHARS = 200
_PDF_MANIFEST_HEAD_PAGES = 10
_PDF_MANIFEST_TAIL_PAGES = 5


def _build_pdf_manifest(data: bytes, *, name: str) -> tuple[str, int]:
    """Return (manifest_text, page_count). Raises _PdfMissingError if pypdf
    isn't installed."""
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError as exc:
        # pypdf is in vibe-cli's base deps; a missing import means the
        # user's install is stale. Suggest the standard refresh path.
        raise _PdfMissingError(
            "PDF attachments need the `pypdf` package, which should be "
            "installed by default. Refresh deps with: `make setup` "
            "(or `uv pip install pypdf`)."
        ) from exc

    import io

    reader = PdfReader(io.BytesIO(data))
    n_pages = len(reader.pages)

    # Metadata (best-effort)
    title = ""
    author = ""
    try:
        meta = reader.metadata or {}
        title = (getattr(meta, "title", None) or meta.get("/Title") or "") or ""
        author = (getattr(meta, "author", None) or meta.get("/Author") or "") or ""
    except Exception:
        pass

    # Outline (TOC) — pypdf returns nested lists of Destination objects.
    toc_lines: list[str] = []
    try:
        outline = getattr(reader, "outline", None) or []
        toc_lines = _flatten_outline(reader, outline)
    except Exception:
        toc_lines = []

    # Per-page text previews.
    def _preview(page_idx: int) -> str:
        try:
            txt = reader.pages[page_idx].extract_text() or ""
        except Exception:
            txt = ""
        txt = " ".join(txt.split())  # collapse whitespace
        return txt[:_PDF_PREVIEW_CHARS]

    preview_lines: list[str] = []
    show_full = n_pages <= (
        _PDF_MANIFEST_HEAD_PAGES + _PDF_MANIFEST_TAIL_PAGES + 5
    )
    if show_full:
        for i in range(n_pages):
            preview_lines.append(f"  p{i + 1}: {_preview(i)!r}")
    else:
        for i in range(_PDF_MANIFEST_HEAD_PAGES):
            preview_lines.append(f"  p{i + 1}: {_preview(i)!r}")
        skipped = n_pages - _PDF_MANIFEST_HEAD_PAGES - _PDF_MANIFEST_TAIL_PAGES
        preview_lines.append(f"  ... ({skipped} more pages) ...")
        for i in range(n_pages - _PDF_MANIFEST_TAIL_PAGES, n_pages):
            preview_lines.append(f"  p{i + 1}: {_preview(i)!r}")

    parts = [f"# attachment: {name} ({n_pages} pages"]
    if title:
        parts.append(f', "{title}"')
    if author:
        parts.append(f' by {author}')
    parts.append(")")
    header = "".join(parts)

    body = [header, ""]
    if toc_lines:
        body.append("TOC:")
        body.extend(f"  {line}" for line in toc_lines[:80])
        if len(toc_lines) > 80:
            body.append(f"  ... ({len(toc_lines) - 80} more entries) ...")
        body.append("")
    body.append("Page previews:")
    body.extend(preview_lines)
    body.append("")
    body.append(
        f'Use read_pdf_pages("{name}", start, end) to read specific pages '
        "(1-indexed). Pass as_image=True on vision-capable models for "
        "figure-heavy pages."
    )

    return "\n".join(body), n_pages


def _flatten_outline(reader, outline, depth: int = 0) -> list[str]:
    """Flatten pypdf's nested outline list into displayable strings.

    pypdf gives us either Destination objects or nested lists. We try to
    resolve each Destination to a page number; failures degrade gracefully.
    """
    lines: list[str] = []
    indent = "  " * depth
    for item in outline:
        if isinstance(item, list):
            lines.extend(_flatten_outline(reader, item, depth + 1))
            continue
        title = getattr(item, "title", None) or str(item)
        page_num = ""
        try:
            page_idx = reader.get_destination_page_number(item)
            if page_idx is not None:
                page_num = f" (p. {page_idx + 1})"
        except Exception:
            pass
        lines.append(f"{indent}{title}{page_num}")
    return lines


# ---------------------------------------------------------
# Full-mode PDF rendering (pymupdf-backed)
# ---------------------------------------------------------
_pymupdf_probe_done: bool = False
_pymupdf_present: bool = False


def _pymupdf_available() -> bool:
    """Lazy one-shot check. Returns False (with one info log) when pymupdf
    isn't importable, so the rest of the code can branch without retrying
    the import on every attach."""
    global _pymupdf_probe_done, _pymupdf_present
    if _pymupdf_probe_done:
        return _pymupdf_present
    try:
        import fitz  # type: ignore[import-not-found]  # noqa: F401  # pymupdf
        _pymupdf_present = True
    except ImportError:
        logger.info(
            "attachment.pymupdf_missing — small-PDF full mode disabled "
            "(install with: uv pip install pymupdf)"
        )
        _pymupdf_present = False
    _pymupdf_probe_done = True
    return _pymupdf_present


def _render_pdf_pages_to_store(
    data: bytes, *, session_id: str, dpi: int
) -> list[str]:
    """Render every page in `data` to PNG via pymupdf, store each in the
    session artifact store, return the list of shas (page-ordered)."""
    import fitz  # type: ignore[import-not-found]

    store = ArtifactStore(session_id)
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        shas: list[str] = []
        for i in range(doc.page_count):
            page = doc.load_page(i)
            pix = page.get_pixmap(dpi=dpi)
            png = pix.tobytes("png")
            ref = store.put(png, kind="attachment.image", ext="png")
            shas.append(ref.sha256)
    finally:
        doc.close()
    return shas


def _extract_pdf_full_text(data: bytes) -> str:
    """Extract the full plaintext content of a PDF via pypdf, page-tagged."""
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        return ""
    import io

    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    for i, page in enumerate(reader.pages, 1):
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
        parts.append(f"--- page {i} ---\n{txt}")
    return "\n\n".join(parts)


# ---------------------------------------------------------
# Bytes re-fetch (used by build_human_content + read_pdf_pages)
# ---------------------------------------------------------
def fetch_bytes(session_id: str, sha: str) -> bytes | None:
    """Re-read attachment bytes from the artifact store. Returns None if not
    found."""
    store = ArtifactStore(session_id)
    return store.read(sha, mode="bytes")  # type: ignore[return-value]


# ---------------------------------------------------------
# Inline path detection
# ---------------------------------------------------------
# Matches likely file-path tokens in a user message. We're conservative:
#   - absolute paths starting with `/` or `~/`
#   - file:// URIs
# Each candidate must be followed by whitespace, end-of-string, or punctuation
# the user wouldn't include in a real filename. Shell-style backslash escapes
# (\ for spaces) are preserved verbatim and decoded in `_normalize_path`.
# A single path "atom": a word/path character OR a `\<space>` shell-escape.
# Critically, an unescaped space ends the path (otherwise consecutive paths
# in one sentence would glue together).
_PATH_ATOM = r"(?:[\w./\-+@%:!~]|\\\s)"

_PATH_PATTERN = _re.compile(
    rf"""
    (?<![/\w])             # not in the middle of another path/word
    (
        file://[^\s'"`<>]+        # explicit URI
        | ~/{_PATH_ATOM}+         # home-relative
        | /{_PATH_ATOM}+          # absolute
    )
    """,
    _re.VERBOSE,
)

# Regions to skip when scanning: inline code fences (`...`) and triple
# backtick code blocks. We don't want to attach paths the user explicitly
# wrote as code.
_BACKTICK_BLOCK = _re.compile(r"```.*?```", _re.DOTALL)
_INLINE_CODE = _re.compile(r"`[^`]*`")


def _normalize_path(token: str) -> str:
    """Decode shell-style backslash-escapes and strip file:// prefix."""
    if token.startswith("file://"):
        token = token[len("file://") :]
    # Shell-escaped spaces, tabs, etc. → real characters.
    return token.replace("\\ ", " ").replace("\\\t", "\t")


def _strip_trailing_punct(token: str) -> str:
    """Drop trailing punctuation a path wouldn't naturally end with."""
    while token and token[-1] in ".,;:!?)]}>\"'":
        token = token[:-1]
    return token


def extract_existing_file_paths(text: str) -> list[tuple[str, Path]]:
    """Return [(original_token, resolved Path)] for every existing-file path
    referenced in `text`. Skips paths inside backtick code spans so users
    can mention paths literally without triggering auto-attach.

    Pure helper — does not touch disk beyond `Path.is_file()` checks.
    """
    if not text:
        return []

    # Blank out code regions so the path regex won't match inside them.
    redacted = _BACKTICK_BLOCK.sub(
        lambda m: " " * len(m.group(0)), text
    )
    redacted = _INLINE_CODE.sub(lambda m: " " * len(m.group(0)), redacted)

    seen: set[str] = set()
    out: list[tuple[str, Path]] = []
    for m in _PATH_PATTERN.finditer(redacted):
        raw = _strip_trailing_punct(m.group(1))
        normalised = _normalize_path(raw)
        try:
            p = Path(normalised).expanduser()
        except (OSError, ValueError):
            continue
        # `is_file()` returns False (no exception) for non-existent paths.
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        out.append((raw, p))
    return out


__all__ = [
    "Attachment",
    "AttachmentError",
    "IMAGE_EXTS",
    "Kind",
    "MAX_ATTACHMENT_BYTES",
    "MAX_TURN_TOTAL_BYTES",
    "PDF_EXTS",
    "TEXT_EXTS",
    "check_turn_budget",
    "detect_kind",
    "extract_existing_file_paths",
    "fetch_bytes",
    "load_attachment",
    "mime_for",
    "total_size",
]
