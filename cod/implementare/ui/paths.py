"""
vibe-cli — Filesystem layout for sessions, backups, artifacts, and preferences.

Sessions are stored per-project under:
    ~/.vibe-cli/sessions/<encoded-cwd>/<uuid7>.json

Pre-compaction snapshots of those session files live under:
    ~/.vibe-cli/session_backups/<encoded-cwd>/<session-uuid>/pre-compaction-<ts>.json

Large tool outputs / file dumps that have been replaced by `@artifact:<sha>`
references in the projected message stream are stored content-addressed under:
    ~/.vibe-cli/artifacts/<session-uuid>/<sha256>.<ext>

The encoded-cwd is the absolute working-directory path with separators
replaced by '-' (matching Claude Code's convention) so each project gets
its own flat folder of session files.
"""

from __future__ import annotations

from pathlib import Path

VIBE_DIR = Path.home() / ".vibe-cli"
USER_CONFIG_PATH = VIBE_DIR / "config.yml"
SESSIONS_DIR = VIBE_DIR / "sessions"
SESSION_BACKUPS_DIR = VIBE_DIR / "session_backups"
ARTIFACTS_DIR = VIBE_DIR / "artifacts"
SKILLS_DIR = VIBE_DIR / "skills"
GLOBAL_VIBE_MD = VIBE_DIR / "VIBE.md"
EVAL_DIR = VIBE_DIR / "eval"
EVAL_DATASETS_DIR = EVAL_DIR / "datasets"
EVAL_RESULTS_DIR = EVAL_DIR / "results"


# ---------------------------------------------------------
# Project-local layout — anchored at cwd (no walk-up).
# ---------------------------------------------------------
PROJECT_VIBE_DIRNAME = ".vibe"


def project_vibe_dir(cwd: Path | str | None = None) -> Path:
    """Return the .vibe directory at the project root (= cwd). Not created."""
    base = Path(cwd) if cwd is not None else Path.cwd()
    return base / PROJECT_VIBE_DIRNAME


def project_skills_dir(cwd: Path | str | None = None) -> Path:
    """Return the per-project skills directory (.vibe/skills). Not created."""
    return project_vibe_dir(cwd) / "skills"


def project_vibe_md(cwd: Path | str | None = None) -> Path:
    """Return the per-project VIBE.md path (.vibe/VIBE.md). Not created."""
    return project_vibe_dir(cwd) / "VIBE.md"


def encode_cwd(cwd: Path | str | None = None) -> str:
    """Encode an absolute path as a single flat folder name."""
    p = Path(cwd) if cwd is not None else Path.cwd()
    s = str(p.resolve())
    if len(s) > 1 and s[1] == ":":
        s = s[0] + s[2:]
    return s.replace("\\", "/").replace("/", "-")


def project_dir(cwd: Path | str | None = None) -> Path:
    """Return the per-project sessions directory (created on demand)."""
    return SESSIONS_DIR / encode_cwd(cwd)


def project_backup_dir(cwd: Path | str | None = None) -> Path:
    """Return the per-project session_backups directory (created on demand)."""
    return SESSION_BACKUPS_DIR / encode_cwd(cwd)


def session_backup_dir(session_id: str, cwd: Path | str | None = None) -> Path:
    """Return the per-session backup directory for one session's snapshots."""
    return project_backup_dir(cwd) / session_id


def session_artifacts_dir(session_id: str) -> Path:
    """Return the per-session artifact directory.

    Artifacts are session-scoped (not project-scoped) because handles are
    embedded in that session's projected message stream — when the session
    is deleted, its artifacts go with it.
    """
    return ARTIFACTS_DIR / session_id
