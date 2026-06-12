"""
vibe-cli — Cross-platform clipboard image reader.

Most terminals don't translate clipboard image data into pasted input —
copy a PNG, hit ⌘V in the prompt, and nothing happens. This module gives
the rest of the app an explicit "grab the image off the clipboard" path
that the `/paste` slash command and the `Ctrl+G` keybinding can use.

Detection strategy is platform-specific and best-effort:

  - macOS  : try `pngpaste -` (Homebrew tool, common); fall back to
             `osascript` reading `«class PNGf»` and base64'ing it.
  - Linux  : try `wl-paste --type image/png` (Wayland); fall back to
             `xclip -selection clipboard -t image/png -o` (X11).
  - Windows: try Pillow's `ImageGrab.grabclipboard()`. Pillow is not a
             base dep but ships with many Python distros; if missing we
             return a "no image" sentinel and the caller prompts.

Always returns `bytes` (PNG-encoded) or `None`. Never raises.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess

logger = logging.getLogger(__name__)


def read_clipboard_image() -> bytes | None:
    """Return PNG-encoded clipboard image bytes, or None if no image present."""
    system = platform.system()
    try:
        if system == "Darwin":
            return _read_macos()
        if system == "Linux":
            return _read_linux()
        if system == "Windows":
            return _read_windows()
    except Exception as exc:
        logger.info("clipboard.read_failed system=%s error=%s", system, exc)
    return None


def clipboard_help_hint() -> str:
    """One-line install hint for the user when no clipboard tool is found."""
    system = platform.system()
    if system == "Darwin":
        return (
            "install with: `brew install pngpaste` (recommended), "
            "or rely on the built-in osascript fallback"
        )
    if system == "Linux":
        return (
            "install one of: `wl-clipboard` (Wayland) or `xclip` (X11)"
        )
    if system == "Windows":
        return "install Pillow: `uv pip install Pillow`"
    return f"unsupported platform: {system}"


# ---------------------------------------------------------
# macOS
# ---------------------------------------------------------
def _read_macos() -> bytes | None:
    # Preferred: pngpaste (fast, clean — used by many devs already).
    if shutil.which("pngpaste"):
        result = subprocess.run(
            ["pngpaste", "-"],
            capture_output=True,
            timeout=5,
        )
        # pngpaste exits non-zero when no image is on the clipboard.
        if result.returncode == 0 and result.stdout:
            return result.stdout
        logger.info("clipboard.pngpaste_empty returncode=%s", result.returncode)
        return None

    # Fallback: osascript. Slower (~150ms) but always available on macOS.
    # AppleScript's byte-handling is awkward; the simplest correct path is
    # to ask it to write the PNG to a temp file we then read back.
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    apple_script = f"""
    try
        set png_data to (the clipboard as «class PNGf»)
        set f to open for access POSIX file "{tmp_path}" with write permission
        write png_data to f
        close access f
        return "ok"
    on error errMsg
        try
            close access POSIX file "{tmp_path}"
        end try
        return "err:" & errMsg
    end try
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", apple_script],
            capture_output=True,
            timeout=5,
            text=True,
        )
        if result.returncode != 0 or not (result.stdout or "").startswith("ok"):
            logger.info("clipboard.osascript_no_image out=%r", result.stdout[:80])
            return None
        from pathlib import Path

        data = Path(tmp_path).read_bytes()
        return data if data else None
    finally:
        try:
            from pathlib import Path

            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------
# Linux
# ---------------------------------------------------------
def _read_linux() -> bytes | None:
    # Wayland first (most modern desktops).
    if shutil.which("wl-paste"):
        result = subprocess.run(
            ["wl-paste", "--type", "image/png"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    # X11 fallback.
    if shutil.which("xclip"):
        result = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    return None


# ---------------------------------------------------------
# Windows
# ---------------------------------------------------------
def _read_windows() -> bytes | None:
    try:
        from PIL import ImageGrab  # type: ignore[import-not-found]
    except ImportError:
        return None
    img = ImageGrab.grabclipboard()
    if img is None:
        return None
    # `grabclipboard` can return a list of file paths (when files are copied)
    # or a PIL Image (when image data is on the clipboard). We only care about
    # the image case here.
    if isinstance(img, list):
        return None
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


__all__ = ["clipboard_help_hint", "read_clipboard_image"]
