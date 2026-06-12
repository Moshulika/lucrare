"""
vibe-cli — Provider-native multimodal content-block construction.

`build_human_content` takes the user's prompt text plus zero-or-more
`Attachment` objects (loaded by `src/attachments.py`) and returns a value
suitable for `HumanMessage(content=...)`:

  - A plain string when no attachments are present (identical to today's
    behaviour — zero churn on existing turns).
  - A list of provider-shaped content blocks otherwise.

PDFs follow a size-aware unified flow (see `docs/MULTIMODAL.md`):

  - **Full mode** (page count ≤ threshold + image-capable model): emit
    extracted text alongside one rendered-PNG image block per page,
    so the document arrives all at once. Same code path on every
    provider — `_image_block_for` handles the per-provider shape.
  - **Manifest mode** (larger PDFs, or non-vision models): emit a TOC
    + per-page-preview text block. The model fetches actual text on
    demand via `read_pdf_pages`, and can ask for rendered pages as
    images via `read_pdf_pages(..., as_image=True)` — those land in
    a follow-up synthesized `HumanMessage` (see `src/main.py`).
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from src.attachments import Attachment, fetch_bytes

logger = logging.getLogger(__name__)


class MultimodalEncodingError(Exception):
    """Raised when an attachment's bytes cannot be encoded for the wire format."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _image_block_for(provider: str, att: Attachment, b64: str) -> dict:
    """Per-provider image content block shape."""
    if provider == "openai":
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{att.mime_type};base64,{b64}"},
        }
    if provider in ("claude", "bedrock"):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": att.mime_type,
                "data": b64,
            },
        }
    if provider == "gemini":
        # langchain-google-genai accepts the OpenAI-style image_url shape.
        return {
            "type": "image_url",
            "image_url": f"data:{att.mime_type};base64,{b64}",
        }
    # Ollama + unknown providers: best-effort, OpenAI-style.
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{att.mime_type};base64,{b64}"},
    }


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def build_human_content(
    text: str,
    attachments: list[Attachment],
    provider: str,
    *,
    session_id: str,
    capability_filter: Any = None,
) -> str | list[dict]:
    """Return the HumanMessage content for a turn.

    Args:
        text: Raw user prompt.
        attachments: Pending attachments for this turn. Empty list → plain string.
        provider: Active provider name (`openai` / `claude` / `bedrock` /
                  `gemini` / `ollama`).
        session_id: Session id — used to re-read attachment bytes from the
                    artifact store (we don't keep them in memory after attach).
        capability_filter: Optional callable `(kind: str) -> bool`. When
                           provided, attachments whose `kind` returns False
                           are *dropped* (with a text-note placeholder added
                           so the model knows what was omitted). Used by
                           session projection to honour learned-bad caps on
                           resume / mid-session model switches.

    Returns:
        `str` if there are no attachments, else `list[dict]` content blocks.

    Raises:
        MultimodalEncodingError: if attachment bytes can't be loaded or
        encoded. The caller (`ui/app.py`) catches this and degrades gracefully.
    """
    if not attachments:
        return text

    blocks: list[dict] = []
    dropped_notes: list[str] = []

    # Text first — predictable ordering for the model.
    for att in attachments:
        if att.kind != "text":
            continue
        if capability_filter and not capability_filter(att.kind):
            dropped_notes.append(
                f"[attachment '{att.name}' omitted by capability filter]"
            )
            continue
        body = att.text_content
        if body is None:
            data = fetch_bytes(session_id, att.sha)
            if data is None:
                dropped_notes.append(
                    f"[attachment '{att.name}' bytes missing from artifact store]"
                )
                continue
            body = data.decode("utf-8", errors="replace")
        blocks.append(_text_block(f"# attachment: {att.name}\n\n{body}\n"))

    # PDFs:
    #   - "full" mode + image-capable model → inline full-text extract
    #     plus one image block per rendered page.
    #   - otherwise → inline the manifest text only; the model uses
    #     read_pdf_pages to fetch text on demand (and can fetch rendered
    #     pages as images via the as_image=True path, which delivers them
    #     in a follow-up synthesized HumanMessage).
    image_ok = (capability_filter is None) or capability_filter("image")
    for att in attachments:
        if att.kind != "pdf":
            continue
        if capability_filter and not capability_filter(att.kind):
            dropped_notes.append(
                f"[attachment '{att.name}' (PDF) omitted by capability filter]"
            )
            continue

        if (
            att.pdf_render_strategy == "full"
            and att.rendered_page_shas
            and image_ok
        ):
            # Full mode: text + every page as an image.
            header = (
                f"# attachment: {att.name} "
                f"({att.pdf_page_count or len(att.rendered_page_shas)} pages, "
                "full mode — text + page images inline)"
            )
            body = att.pdf_full_text or ""
            blocks.append(_text_block(f"{header}\n\n{body}\n"))
            for sha in att.rendered_page_shas:
                data = fetch_bytes(session_id, sha)
                if data is None:
                    dropped_notes.append(
                        f"[rendered page from '{att.name}' missing from artifact store]"
                    )
                    continue
                try:
                    b64 = _b64(data)
                except Exception as exc:
                    raise MultimodalEncodingError(
                        f"failed to base64-encode page from {att.name}: {exc}"
                    ) from exc
                # Reuse the per-provider image-block shaping.
                img_att = Attachment(
                    path="",
                    name=f"{att.name}#page",
                    kind="image",
                    mime_type="image/png",
                    size=len(data),
                    sha=sha,
                    ext="png",
                )
                blocks.append(_image_block_for(provider, img_att, b64))
            continue

        # Fallback: manifest text only.
        manifest = att.pdf_manifest or f"# attachment: {att.name} (PDF)"
        blocks.append(_text_block(manifest))

    # User prompt last so it's the model's most recent instruction.
    if text:
        blocks.append(_text_block(text))

    # Images at the end — most providers want them adjacent to the question.
    for att in attachments:
        if att.kind != "image":
            continue
        if capability_filter and not capability_filter(att.kind):
            dropped_notes.append(
                f"[image '{att.name}' omitted: active model doesn't accept image input]"
            )
            continue
        data = fetch_bytes(session_id, att.sha)
        if data is None:
            dropped_notes.append(
                f"[image '{att.name}' bytes missing from artifact store]"
            )
            continue
        try:
            b64 = _b64(data)
        except Exception as exc:
            raise MultimodalEncodingError(
                f"failed to base64-encode {att.name}: {exc}"
            ) from exc
        blocks.append(_image_block_for(provider, att, b64))

    if dropped_notes:
        blocks.append(_text_block("\n".join(dropped_notes)))

    if not blocks:
        # Pathological case — all attachments dropped, no text. Return a
        # placeholder so we never end up with HumanMessage(content=[]).
        return text or "(no content)"

    return blocks


def build_image_blocks_from_shas(
    shas: list[str],
    *,
    provider: str,
    session_id: str,
    mime_type: str = "image/png",
) -> list[dict]:
    """Build a list of provider-shaped image content blocks from artifact shas.

    Used by `read_pdf_pages(as_image=True)` to stage rendered pages for the
    synthesized-HumanMessage injection drain in `src/main.py`. Skips shas
    whose bytes can't be loaded (logs once each) rather than failing the
    whole batch."""
    blocks: list[dict] = []
    for sha in shas:
        data = fetch_bytes(session_id, sha)
        if data is None:
            logger.info("multimodal.injection_missing sha=%s", sha[:12])
            continue
        try:
            b64 = _b64(data)
        except Exception as exc:
            logger.info(
                "multimodal.injection_encode_failed sha=%s error=%s",
                sha[:12],
                exc,
            )
            continue
        # Reuse the per-provider image-block shaping by faking a minimal
        # Attachment shell — only the mime_type is read by `_image_block_for`.
        shell = Attachment(
            path="",
            name="rendered_page.png",
            kind="image",
            mime_type=mime_type,
            size=len(data),
            sha=sha,
            ext="png",
        )
        blocks.append(_image_block_for(provider, shell, b64))
    return blocks


def event_blocks_for_attachments(attachments: list[Attachment]) -> list[dict]:
    """Serialise pending attachments into the session event-log block shape."""
    return [att.to_block() for att in attachments]


def build_human_event_content(
    text: str, attachments: list[Attachment]
) -> str | list[dict]:
    """Return the value stored on the `KIND_HUMAN` session event.

    Plain string when no attachments — fully backward-compatible with
    pre-multimodal sessions. Otherwise a list combining the user text and
    one "attachment" block per file (artifact-marker form, no raw bytes)."""
    if not attachments:
        return text
    blocks: list[dict] = [{"type": "text", "text": text}] if text else []
    blocks.extend(event_blocks_for_attachments(attachments))
    return blocks


__all__ = [
    "MultimodalEncodingError",
    "build_human_content",
    "build_human_event_content",
    "build_image_blocks_from_shas",
    "event_blocks_for_attachments",
]
