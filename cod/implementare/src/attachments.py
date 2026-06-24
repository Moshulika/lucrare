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

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_TURN_TOTAL_BYTES = 20 * 1024 * 1024  # 20 MB

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
PDF_EXTS = {".pdf"}
TEXT_EXTS = {
    ".txt",".md",".csv",".json",".log",
    ".yaml",".yml",".toml",".ini",".cfg",".py",
}


class AttachmentError(Exception):
    ...

@dataclass
class Attachment:
    path: str 
    name: str 
    kind: Kind
    mime_type: str
    size: int
    sha: str
    ext: str 
    text_content: str | None = None 
    pdf_manifest: str | None = None
    pdf_page_count: int | None = None
    pdf_render_strategy: str = "manifest"
    rendered_page_shas: list[str] = field(default_factory=list)
    pdf_full_text: str | None = None
    extra: dict = field(default_factory=dict)

    def to_block(self) -> dict:
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
        return cls(
            path="", 
            name=block.get("name", ""),
            kind=block.get("kind", "text"),
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

def detect_kind(path: Path | str) -> Kind | None:
    p = Path(path)
    ext = p.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in TEXT_EXTS:
        return "text"
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
    mt, _ = mimetypes.guess_type(str(path))
    if mt:
        return mt
    if kind == "image":
        return f"image/{path.suffix.lstrip('.').lower() or 'png'}"
    if kind == "pdf":
        return "application/pdf"
    return "text/plain"

def load_attachment(
    path: Path | str,
    *,
    session_id: str,
    image_capable: bool = True,
    small_threshold_pages: int = 15,
    render_dpi: int = 144,
) -> Attachment:
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

    store = ArtifactStore(session_id)
    store.put(data, kind=f"attachment.{kind}", ext=ext)

    text_content: str | None = None
    pdf_manifest: str | None = None
    pdf_page_count: int | None = None

    if kind == "text":
        try:
            text_content = data.decode("utf-8")
        except UnicodeDecodeError:
            # Best-effort decode - show what we can, but warn.
            text_content = data.decode("utf-8", errors="replace")
            logger.info("attachment.text_decode_replace path=%s", p)
    elif kind == "pdf":
        try:
            pdf_manifest, pdf_page_count = _build_pdf_manifest(data, name=p.name)
        except _PdfMissingError as exc:
            raise AttachmentError(str(exc)) from exc
        except Exception as exc:  # pypdf-side failure
            raise AttachmentError(f"failed to parse {p.name}: {exc}") from exc

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
            logger.info(
                "attachment.pdf_render_failed path=%s error=%s - falling back to manifest mode",
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
    if total_size(pending) + new_size > MAX_TURN_TOTAL_BYTES:
        mb = MAX_TURN_TOTAL_BYTES // (1024 * 1024)
        raise AttachmentError(
            f"total attachments would exceed the per-turn cap of {mb} MB"
        )

class _PdfMissingError(Exception):
    pass

_PDF_PREVIEW_CHARS = 200
_PDF_MANIFEST_HEAD_PAGES = 10
_PDF_MANIFEST_TAIL_PAGES = 5

def _build_pdf_manifest(data: bytes, *, name: str) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise _PdfMissingError(
            "PDF attachments need the `pypdf` package, which should be "
            "installed by default. Refresh deps with: `make setup` "
            "(or `uv pip install pypdf`)."
        ) from exc

    import io
    reader = PdfReader(io.BytesIO(data))
    n_pages = len(reader.pages)
    title = ""
    author = ""
    try:
        meta = reader.metadata or {}
        title = (getattr(meta, "title", None) or meta.get("/Title") or "") or ""
        author = (getattr(meta, "author", None) or meta.get("/Author") or "") or ""
    except Exception:
        pass
    toc_lines: list[str] = []
    try:
        outline = getattr(reader, "outline", None) or []
        toc_lines = _flatten_outline(reader, outline)
    except Exception:
        toc_lines = []

    def _preview(page_idx: int) -> str:
        try:
            txt = reader.pages[page_idx].extract_text() or ""
        except Exception:
            txt = ""
        txt = " ".join(txt.split()) 
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

_pymupdf_probe_done: bool = False
_pymupdf_present: bool = False


def _pymupdf_available() -> bool:
    global _pymupdf_probe_done, _pymupdf_present
    if _pymupdf_probe_done:
        return _pymupdf_present
    try:
        import fitz 
        _pymupdf_present = True
    except ImportError:
        logger.info(
            "attachment.pymupdf_missing - small-PDF full mode disabled "
            "(install with: uv pip install pymupdf)"
        )
        _pymupdf_present = False
    _pymupdf_probe_done = True
    return _pymupdf_present


def _render_pdf_pages_to_store(
    data: bytes, *, session_id: str, dpi: int
) -> list[str]:
    import fitz

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
    try:
        from pypdf import PdfReader 
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

def fetch_bytes(session_id: str, sha: str) -> bytes | None:
    store = ArtifactStore(session_id)
    return store.read(sha, mode="bytes")

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

_BACKTICK_BLOCK = _re.compile(r"```.*?```", _re.DOTALL)
_INLINE_CODE = _re.compile(r"`[^`]*`")

def _normalize_path(token: str) -> str:
    if token.startswith("file://"):
        token = token[len("file://") :]
    return token.replace("\\ ", " ").replace("\\\t", "\t")


def _strip_trailing_punct(token: str) -> str:
    while token and token[-1] in ".,;:!?)]}>\"'":
        token = token[:-1]
    return token


def extract_existing_file_paths(text: str) -> list[tuple[str, Path]]:
    if not text:
        return []
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
    "Attachment","AttachmentError","IMAGE_EXTS",
    "Kind","MAX_ATTACHMENT_BYTES","MAX_TURN_TOTAL_BYTES",
    "PDF_EXTS","TEXT_EXTS","check_turn_budget",
    "detect_kind","extract_existing_file_paths","fetch_bytes",
    "load_attachment","mime_for","total_size",
]
