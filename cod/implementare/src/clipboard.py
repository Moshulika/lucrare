from __future__ import annotations
import logging
import platform
import shutil
import subprocess
logger = logging.getLogger(__name__)

def read_clipboard_image() -> bytes | None:
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

def _read_macos() -> bytes | None:
    if shutil.which("pngpaste"):
        result = subprocess.run(
            ["pngpaste", "-"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
        logger.info("clipboard.pngpaste_empty returncode=%s", result.returncode)
        return None
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

def _read_linux() -> bytes | None:
    if shutil.which("wl-paste"):
        result = subprocess.run(
            ["wl-paste", "--type", "image/png"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    if shutil.which("xclip"):
        result = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    return None

def _read_windows() -> bytes | None:
    try:
        from PIL import ImageGrab 
    except ImportError:
        return None
    img = ImageGrab.grabclipboard()
    if img is None:
        return None
    if isinstance(img, list):
        return None
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

__all__ = ["clipboard_help_hint", "read_clipboard_image"]
