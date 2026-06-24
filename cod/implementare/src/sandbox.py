from __future__ import annotations
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_MARKERS = (
    ".git","pyproject.toml","package.json",
    "requirements.txt","Makefile",".vibe-cli",
)

_HARD_DENY_ABSOLUTE = {"/", "/tmp", "/var/tmp"}

_HOME_PERSONAL_SUBDIRS = {
    "Desktop","Documents","Downloads",
    "Movies","Music","Pictures","Library",
}

_AMBIGUOUS_MAX_MB = 500
_AMBIGUOUS_MAX_FILES = 10_000


@dataclass
class PolicyResult:
    allow: bool
    reason: str
    silent: bool = True  

def _is_hard_denied(cwd: Path, home: Path) -> bool:
    if str(cwd) in _HARD_DENY_ABSOLUTE:
        return True
    if cwd == home:
        return True
    parts = cwd.parts
    if len(parts) == 3 and parts[0] == "/" and parts[1] in ("Users", "home"):
        return True
    if cwd.parent == home and cwd.name in _HOME_PERSONAL_SUBDIRS:
        return True
    return False


def _has_project_marker(cwd: Path) -> bool:
    return any((cwd / m).exists() for m in PROJECT_MARKERS)


def _estimate_size(cwd: Path) -> tuple[int, int]:
    mb = 0
    file_count = 0
    if sys.platform != "win32":
        try:
            r = subprocess.run(
                ["du", "-sk", str(cwd)],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if r.returncode == 0 and r.stdout.strip():
                kb = int(r.stdout.split()[0])
                mb = kb // 1024
        except Exception:
            pass
        try:
            r = subprocess.run(
                ["find", str(cwd), "-type", "f"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if r.returncode == 0:
                file_count = r.stdout.count("\n")
        except Exception:
            pass
    return mb, file_count

def cwd_policy(
    cwd: Path,
    max_mb: int = _AMBIGUOUS_MAX_MB,
    max_files: int = _AMBIGUOUS_MAX_FILES,
) -> PolicyResult:
    
    home = Path.home()

    if os.environ.get("VIBE_SANDBOX_FORCE") == "1":
        return PolicyResult(
            allow=True,
            reason="VIBE_SANDBOX_FORCE=1 — policy bypassed",
            silent=False,
        )

    if _is_hard_denied(cwd, home):
        return PolicyResult(
            allow=False,
            reason=(
                f"cwd {cwd} looks like a personal directory tree, not a "
                f"project. Falling back to host execution. "
                f"(Set VIBE_SANDBOX_FORCE=1 to override.)"
            ),
            silent=False,
        )

    if _has_project_marker(cwd):
        return PolicyResult(
            allow=True,
            reason="project markers detected",
            silent=True,
        )

    mb, files = _estimate_size(cwd)
    if mb > max_mb or files > max_files:
        return PolicyResult(
            allow=False,
            reason=(
                f"cwd {cwd} has no project markers and is large "
                f"({mb} MB, {files} files). Falling back to host execution. "
                f"(Set VIBE_SANDBOX_FORCE=1 to override.)"
            ),
            silent=False,
        )

    return PolicyResult(
        allow=True,
        reason=(
            f"Sandboxing {cwd} (no project markers detected, "
            f"{mb} MB / {files} files)."
        ),
        silent=False,
    )

def docker_available() -> tuple[bool, str]:
    """Return (ok, reason). Never raises."""
    if shutil.which("docker") is None:
        return False, "docker CLI not found in PATH"
    try:
        r = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except subprocess.TimeoutExpired:
        return False, "docker info timed out (daemon not responding)"
    except Exception as e:
        return False, f"docker info failed: {e}"
    if r.returncode != 0:
        first = (r.stderr or "").strip().splitlines()[:1]
        return False, first[0] if first else "docker daemon unreachable"
    return True, "ok"

@dataclass
class RunResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False

class Sandbox:

    def __init__(
        self,
        container_id: str,
        workspace: Path,
        image: str,
        timeout_default: int,
    ):
        self.container_id = container_id
        self.workspace = workspace
        self.image = image
        self.timeout_default = timeout_default
        self._closed = False

    @property
    def short_id(self) -> str:
        return self.container_id[:12]

    @classmethod
    def try_start(
        cls, cfg: dict, workspace: Path
    ) -> tuple["Sandbox | None", str]:
        ok, why = docker_available()
        if not ok:
            return None, why

        policy = cwd_policy(
            workspace,
            max_mb=int(cfg.get("ambiguous_max_mb") or _AMBIGUOUS_MAX_MB),
            max_files=int(cfg.get("ambiguous_max_files") or _AMBIGUOUS_MAX_FILES),
        )
        if not policy.allow:
            return None, policy.reason

        image = cfg.get("image") or "python:3.13-slim"
        network = cfg.get("network") or "bridge"
        memory = str(cfg.get("memory") or "2g")
        cpus = str(cfg.get("cpus") or 2)
        timeout_default = int(cfg.get("timeout_default") or 30)

        cmd = [
            "docker","run","-d","--rm","-v",
            f"{workspace.resolve()}:/workspace:rw",
            "-w","/workspace","--network",str(network),
            "--memory",memory,"--cpus",cpus,"--cap-drop","ALL",
            "--user",f"{os.getuid()}:{os.getgid()}" if sys.platform != "win32" else "0:0",
            image,"sleep","infinity",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return None, "docker run timed out (image pull may be in progress)"
        except Exception as e:
            return None, f"docker run failed: {e}"

        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip().splitlines()[:1]
            return None, err[0] if err else "docker run failed (no output)"

        cid = r.stdout.strip()
        if not cid:
            return None, "docker run returned no container id"

        notice = policy.reason if not policy.silent else ""
        return cls(cid, workspace, image, timeout_default), notice or "ok"

    def run(self, command: str, timeout: int | None = None) -> RunResult:
        if self._closed:
            return RunResult(
                stdout="",
                stderr="sandbox is closed",
                exit_code=-1,
            )
        t = int(timeout if timeout is not None else self.timeout_default)
        try:
            r = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-w",
                    "/workspace",
                    self.container_id,
                    "sh",
                    "-c",
                    command,
                ],
                capture_output=True,
                text=True,
                timeout=t,
            )
            return RunResult(
                stdout=r.stdout or "",
                stderr=r.stderr or "",
                exit_code=r.returncode,
            )
        except subprocess.TimeoutExpired as e:
            return RunResult(
                stdout=e.stdout.decode(errors="replace") if e.stdout else "",
                stderr=e.stderr.decode(errors="replace") if e.stderr else "",
                exit_code=-1,
                timed_out=True,
            )
        except Exception as e:
            return RunResult(stdout="", stderr=f"sandbox exec error: {e}", exit_code=-1)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            subprocess.run(
                ["docker", "kill", self.container_id],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass