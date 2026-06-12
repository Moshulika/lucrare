"""Live local hardware/software snapshot — never persisted to disk."""
from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore


def snapshot() -> dict[str, Any]:
    info: dict[str, Any] = {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor() or platform.machine(),
        "python": sys.version.split()[0],
        "python_impl": platform.python_implementation(),
    }
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            info["cpu_logical"] = psutil.cpu_count(logical=True)
            info["cpu_physical"] = psutil.cpu_count(logical=False)
            info["memory_total"] = vm.total
            info["memory_available"] = vm.available
        except Exception:
            pass
    info["vibe_cli_rev"] = _git_rev()
    return info


def _git_rev() -> str | None:
    repo = Path(__file__).resolve().parents[2]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None
