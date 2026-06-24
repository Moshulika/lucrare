from __future__ import annotations
import base64
import logging
from typing import Any
from src.attachments import Attachment, fetch_bytes
logger = logging.getLogger(__name__)

class MultimodalEncodingError(Exception):
    ...

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")

def _image_block_for(provider: str, att: Attachment, b64: str) -> dict:
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
        return {
            "type": "image_url",
            "image_url": f"data:{att.mime_type};base64,{b64}",
        }
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
    if not attachments:
        return text

    blocks: list[dict] = []
    dropped_notes: list[str] = []

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
            header = (
                f"# attachment: {att.name} "
                f"({att.pdf_page_count or len(att.rendered_page_shas)} pages, "
                "full mode - text + page images inline)"
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

        manifest = att.pdf_manifest or f"# attachment: {att.name} (PDF)"
        blocks.append(_text_block(manifest))

    if text:
        blocks.append(_text_block(text))

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
        return text or "(no content)"

    return blocks

def build_image_blocks_from_shas(
    shas: list[str],
    *,
    provider: str,
    session_id: str,
    mime_type: str = "image/png",
) -> list[dict]:
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
    return [att.to_block() for att in attachments]

def build_human_event_content(
    text: str, attachments: list[Attachment]
) -> str | list[dict]:
    if not attachments:
        return text
    blocks: list[dict] = [{"type": "text", "text": text}] if text else []
    blocks.extend(event_blocks_for_attachments(attachments))
    return blocks

__all__ = [
    "MultimodalEncodingError","build_human_content",
    "build_human_event_content","build_image_blocks_from_shas",
    "event_blocks_for_attachments",
]
