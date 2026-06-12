"""
vibe-cli — git updater.

Compares the local checkout against its upstream branch (no fetch — uses
`git ls-remote`) and exposes an apply path that fast-forwards + reinstalls.
The /update command drives this; the bottom toolbar reads `UpdateInfo` to
surface a one-line hint when an update is available.
"""

from __future__ import annotations

import asyncio
import importlib.metadata as _importlib_metadata
import json as _json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def launch_command() -> str:
    """Return the shell command a user should run to restart vibe-cli."""
    argv0 = sys.argv[0] if sys.argv else ""
    if Path(argv0).name in ("vibe", "vibe.exe"):
        return "vibe"
    return "make ui"


@dataclass
class UpdateInfo:
    branch: str
    local: str
    remote: str
    behind: int
    ahead: int
    dirty: bool
    behind_known: bool = field(default=True)

    @property
    def available(self) -> bool:
        return self.behind > 0

    @property
    def can_fast_forward(self) -> bool:
        return self.behind > 0 and self.ahead == 0 and not self.dirty

    @property
    def behind_str(self) -> str:
        """Human-readable description of how far behind we are."""
        if not self.behind_known:
            return "updates available"
        return f"{self.behind} new commit(s)"


@dataclass
class UpdateResult:
    ok: bool
    message: str
    details: str = ""


def _run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return -1, "", ""


def _is_git_repo() -> bool:
    rc, _, _ = _run(["git", "rev-parse", "--is-inside-work-tree"])
    return rc == 0


def _upstream_branch() -> str | None:
    """Resolve the branch to compare against.

    Prefers the explicit upstream (`@{u}`); falls back to the remote's
    default branch (`origin/HEAD`) so a freshly cloned checkout works
    even when the local branch hasn't been configured to track anything.
    """
    rc, out, _ = _run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
    )
    if rc == 0 and out:
        return out
    rc, out, _ = _run(
        ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"]
    )
    if rc == 0 and out and out != "origin/HEAD":
        return out
    return None


def _local_head() -> str | None:
    rc, out, _ = _run(["git", "rev-parse", "HEAD"])
    return out if rc == 0 and out else None


def _remote_head(upstream: str) -> str | None:
    if "/" not in upstream:
        return None
    remote, branch = upstream.split("/", 1)
    rc, out, _ = _run(
        ["git", "ls-remote", remote, f"refs/heads/{branch}"], timeout=15.0
    )
    if rc != 0 or not out:
        return None
    return out.split()[0]


def _is_dirty() -> bool:
    rc, out, _ = _run(["git", "status", "--porcelain"])
    return rc == 0 and bool(out)


def _ahead_behind(local: str, remote: str) -> tuple[int, int]:
    rc, out, _ = _run(
        ["git", "rev-list", "--left-right", "--count", f"{local}...{remote}"]
    )
    if rc != 0 or not out:
        return 0, 0
    parts = out.split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def _package_install_info() -> tuple[str, str, str]:
    """Inspect the vibe-cli dist-info to determine how the package was installed.

    Returns (mode, url, revision):
      mode:     "git_url" | "local_dir" | "pypi" | "unknown"
      url:      git/file URL or ""
      revision: requested git branch/tag, or ""
    """
    try:
        dist = _importlib_metadata.Distribution.from_name("vibe-cli")
        for f in dist.files or []:
            if f.name == "direct_url.json":
                data = _json.loads(f.locate().read_text())
                url = data.get("url", "")
                if "vcs_info" in data and data["vcs_info"].get("vcs") == "git":
                    rev = data["vcs_info"].get("requested_revision") or ""
                    return "git_url", url, rev
                if "dir_info" in data:
                    return "local_dir", url, ""
        return "pypi", "", ""
    except Exception:
        return "unknown", "", ""


def _find_pip() -> list[str]:
    """Return a pip invocation — prefer uv pip, fall back to the current interpreter's pip."""
    if shutil.which("uv"):
        return ["uv", "pip"]
    return [sys.executable, "-m", "pip"]


def _check_git_url_update(url: str, revision: str, commit_id: str) -> UpdateInfo | None:
    """Check for an update when the package was installed via pip install git+<url>."""
    branch = revision or "main"
    rc, out, _ = _run(["git", "ls-remote", url, f"refs/heads/{branch}"], timeout=15.0)
    if rc != 0 or not out:
        return None
    remote_commit = out.split()[0]
    behind = 0 if commit_id == remote_commit else 1
    return UpdateInfo(
        branch=branch,
        local=commit_id,
        remote=remote_commit,
        behind=behind,
        ahead=0,
        dirty=False,
        behind_known=False,
    )


def _apply_package_update() -> UpdateResult:
    """Upgrade vibe-cli via pip/uv for non-git-checkout installs."""
    mode, url, _ = _package_install_info()
    pip = _find_pip()

    if mode == "git_url":
        cmd = pip + ["install", "--upgrade", f"git+{url}"]
    elif mode == "pypi":
        cmd = pip + ["install", "--upgrade", "vibe-cli"]
    elif mode == "local_dir":
        return UpdateResult(
            False,
            "Installed from a local directory — pull changes there and re-run the install.",
        )
    else:
        return UpdateResult(
            False,
            "Not a git checkout and install source is unknown — update manually.",
        )

    rc, out, err = _run(cmd, timeout=120.0)
    if rc != 0:
        return UpdateResult(False, "Package upgrade failed.", details=(out + "\n" + err).strip())
    return UpdateResult(True, "Updated. Restart vibe-cli to load the new code.")


def check_for_update_sync() -> UpdateInfo | None:
    """Compare the local version against the upstream and return update state.

    For git checkouts: compares HEAD against the upstream via `git ls-remote`
    (no fetch — never mutates the working tree).
    For pip/uv installs from a git URL: compares the installed commit against
    the remote branch HEAD.
    Returns None when state can't be determined.
    """
    if not _is_git_repo():
        mode, url, revision = _package_install_info()
        if mode != "git_url":
            return None
        try:
            dist = _importlib_metadata.Distribution.from_name("vibe-cli")
            commit_id = ""
            for f in dist.files or []:
                if f.name == "direct_url.json":
                    data = _json.loads(f.locate().read_text())
                    commit_id = data.get("vcs_info", {}).get("commit_id", "")
                    break
        except Exception:
            return None
        if not commit_id:
            return None
        return _check_git_url_update(url, revision, commit_id)
    upstream = _upstream_branch()
    if not upstream:
        return None
    local = _local_head()
    if not local:
        return None
    remote = _remote_head(upstream)
    if not remote:
        return None

    branch = upstream.split("/", 1)[1] if "/" in upstream else upstream
    if local == remote:
        return UpdateInfo(
            branch=branch,
            local=local,
            remote=remote,
            behind=0,
            ahead=0,
            dirty=False,
        )

    ahead, behind = _ahead_behind(local, remote)
    return UpdateInfo(
        branch=branch,
        local=local,
        remote=remote,
        behind=behind,
        ahead=ahead,
        dirty=_is_dirty(),
    )


async def check_for_update() -> UpdateInfo | None:
    return await asyncio.to_thread(check_for_update_sync)


def apply_update_sync() -> UpdateResult:
    """Update vibe-cli. For git checkouts: fast-forward pull + `make setup`.
    For pip/uv installs: upgrades via the appropriate package manager."""
    if not _is_git_repo():
        return _apply_package_update()

    if _is_dirty():
        return UpdateResult(
            False,
            "Working tree has uncommitted changes. "
            "Commit or stash them, then run /update again.",
        )

    rc, out, err = _run(["git", "pull", "--ff-only"], timeout=60.0)
    if rc != 0:
        return UpdateResult(
            False,
            "git pull --ff-only failed — local branch likely diverged from "
            "remote. Rebase or merge manually.",
            details=(out + "\n" + err).strip(),
        )

    rc, out2, err2 = _run(["make", "install"], timeout=600.0)
    if rc != 0:
        return UpdateResult(
            False,
            "git pull succeeded, but `make setup` failed. "
            "Fix the install error and restart manually.",
            details=(out2 + "\n" + err2).strip(),
        )

    return UpdateResult(
        True,
        "Updated. Restart vibe-cli to load the new code.",
        details=(out + "\n" + out2).strip(),
    )


async def apply_update() -> UpdateResult:
    return await asyncio.to_thread(apply_update_sync)