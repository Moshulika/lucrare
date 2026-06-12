"""
vibe-cli — Content-addressed artifact store.

Large tool outputs (file reads, search results, command output) that would
otherwise dominate the LLM context can be moved here and replaced in the
projected message stream with a small handle:

    @artifact:<sha256-prefix>

The `fetch_artifact` tool lets the agent re-hydrate a payload (optionally a
byte range) when it actually needs the content again.

Layout
------
    ~/.vibe-cli/artifacts/<session-id>/<sha256>.<ext>
    ~/.vibe-cli/artifacts/<session-id>/<sha256>.meta.json   (kind, bytes, ts)

Storage is sha256-keyed so identical payloads stored twice within a session
deduplicate naturally on disk.

Lifecycle
---------
- TTL: 30 days from last-modified time.
- Delete-on-session-delete: when a session file is removed, callers should
  invoke `purge_session(session_id)` to drop its artifact directory.
- GC is lazy and stat-based — `gc_expired()` walks the artifact root once,
  triggered opportunistically (e.g. on session open). There is no daemon.

Future work
-----------
- Cross-session deduplication (a single global content-addressed pool with
  per-session reference counting). Skipped for now to keep deletion simple.
- Compression of stored payloads for large text artifacts.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from langchain_core.tools import tool

from ui.paths import ARTIFACTS_DIR, session_artifacts_dir

# 30 days, in seconds. Documented in the module docstring.
ARTIFACT_TTL_SECONDS = 30 * 24 * 60 * 60

HANDLE_PREFIX = "@artifact:"
SHA_PREFIX_LEN = 12  # short handle length for in-context references


# ---------------------------------------------------------
# Data
# ---------------------------------------------------------
@dataclass(frozen=True)
class ArtifactRef:
    """A handle pointing at a stored artifact payload."""

    session_id: str
    sha256: str
    kind: str
    bytes_: int
    ext: str

    @property
    def short(self) -> str:
        return self.sha256[:SHA_PREFIX_LEN]

    @property
    def handle(self) -> str:
        return f"{HANDLE_PREFIX}{self.short}"

    def to_meta(self) -> dict:
        return {
            "sha256": self.sha256,
            "kind": self.kind,
            "bytes": self.bytes_,
            "ext": self.ext,
            "ts": time.time(),
        }


# ---------------------------------------------------------
# Store
# ---------------------------------------------------------
class ArtifactStore:
    """Per-session content-addressed store."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.root = session_artifacts_dir(session_id)

    def _payload_path(self, sha: str, ext: str) -> Path:
        return self.root / f"{sha}.{ext}"

    def _meta_path(self, sha: str) -> Path:
        return self.root / f"{sha}.meta.json"

    def put(
        self,
        payload: str | bytes,
        *,
        kind: str = "text",
        ext: str = "txt",
    ) -> ArtifactRef:
        """Write a payload, returning its ref. Idempotent on identical bytes."""
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        sha = hashlib.sha256(data).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)

        ppath = self._payload_path(sha, ext)
        if not ppath.exists():
            tmp = ppath.with_suffix(ppath.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(ppath)

        ref = ArtifactRef(
            session_id=self.session_id,
            sha256=sha,
            kind=kind,
            bytes_=len(data),
            ext=ext,
        )
        mpath = self._meta_path(sha)
        if not mpath.exists():
            mpath.write_text(json.dumps(ref.to_meta(), indent=2))
        return ref

    def resolve(self, short_or_full: str) -> ArtifactRef | None:
        """Resolve a short or full sha to a ref by scanning meta files."""
        if not self.root.exists():
            return None
        prefix = short_or_full
        for meta_file in self.root.glob("*.meta.json"):
            sha = meta_file.name[: -len(".meta.json")]
            if sha.startswith(prefix):
                meta = json.loads(meta_file.read_text())
                return ArtifactRef(
                    session_id=self.session_id,
                    sha256=meta["sha256"],
                    kind=meta.get("kind", "text"),
                    bytes_=meta.get("bytes", 0),
                    ext=meta.get("ext", "txt"),
                )
        return None

    def read(
        self,
        short_or_full: str,
        *,
        mode: Literal["text", "bytes"] = "text",
        byte_range: tuple[int, int] | None = None,
    ) -> str | bytes | None:
        ref = self.resolve(short_or_full)
        if ref is None:
            return None
        ppath = self._payload_path(ref.sha256, ref.ext)
        if not ppath.exists():
            return None
        if byte_range is None:
            data = ppath.read_bytes()
        else:
            start, end = byte_range
            with ppath.open("rb") as fh:
                fh.seek(max(0, start))
                data = fh.read(max(0, end - start))
        return data.decode("utf-8", errors="replace") if mode == "text" else data


# ---------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------
def purge_session(session_id: str) -> None:
    """Remove a session's entire artifact directory.

    Call this from the session-delete path so artifacts don't outlive the
    session that referenced them.
    """
    d = session_artifacts_dir(session_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def gc_expired(now: float | None = None) -> int:
    """Lazy garbage collect artifact dirs older than ARTIFACT_TTL_SECONDS.

    Returns the number of session directories removed. Stat-based, no daemon —
    intended to be invoked opportunistically (e.g. once on session open).
    """
    if not ARTIFACTS_DIR.exists():
        return 0
    cutoff = (now if now is not None else time.time()) - ARTIFACT_TTL_SECONDS
    removed = 0
    for sess_dir in ARTIFACTS_DIR.iterdir():
        if not sess_dir.is_dir():
            continue
        try:
            mtime = sess_dir.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            shutil.rmtree(sess_dir, ignore_errors=True)
            removed += 1
    return removed


# ---------------------------------------------------------
# Tool exposed to the agent
# ---------------------------------------------------------
def make_fetch_artifact_tool(session_id: str):
    """Build a `fetch_artifact` tool bound to a specific session's store.

    The tool is built per-session because handles are session-scoped. Wire
    this into the agent's tool list at session start.
    """
    store = ArtifactStore(session_id)

    @tool
    def fetch_artifact(handle: str, start: int = 0, length: int = 0) -> str:
        """Re-fetch a previously stored artifact by its `@artifact:<sha>` handle.

        Args:
            handle: The handle string (with or without the `@artifact:` prefix).
            start: Byte offset to start reading from (default 0).
            length: Number of bytes to read (default 0 = full payload).
        """
        h = handle.removeprefix(HANDLE_PREFIX).strip()
        if not h:
            return "Error: empty artifact handle."
        byte_range = (start, start + length) if length > 0 else None
        data = store.read(h, mode="text", byte_range=byte_range)
        if data is None:
            return f"Artifact not found: {handle}"
        return data  # type: ignore[return-value]

    return fetch_artifact


__all__ = [
    "ArtifactRef",
    "ArtifactStore",
    "HANDLE_PREFIX",
    "SHA_PREFIX_LEN",
    "ARTIFACT_TTL_SECONDS",
    "purge_session",
    "gc_expired",
    "make_fetch_artifact_tool",
]