"""
vibe-cli — Skills (global, file-backed, progressively disclosed).

A skill is a single Markdown file under ~/.vibe-cli/skills/<name>.md with a
small YAML frontmatter block:

    ---
    name: pr-review
    description: Review code for bugs, style, and best practices.
    when_to_use: When the user asks for a code review or feedback on a diff.
    enabled: false
    ---
    <body — full instructions the model follows once `run_skill` returns them>

The system prompt only ever sees the **directory** (name + description +
when_to_use) of enabled skills — bodies stay on disk until the model invokes
the `run_skill` tool, which reads them from this index. This is the
"progressive disclosure" pattern: cheap context, model decides when to load.

The index is loaded once at startup into `ctx["skills_index"]` and rebuilt
on `/skills reload` or after a toggle.

A configurable cap (`skills.max_active`, default 10) bounds how many enabled
skills the model can see. Skills past the cap are surfaced to the user as
"dropped" but invisible to the agent. Sorting is by name (alphabetical) for
predictability — first N alphabetically win.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ui.paths import SKILLS_DIR, project_skills_dir


DEFAULT_MAX_ACTIVE = 10

# Hand-rolled frontmatter splitter — avoids pulling in python-frontmatter
# for one feature. Matches files starting with `---\n...\n---\n`.
_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<frontmatter>.*?)\n---\s*\n?(?P<body>.*)\Z",
    re.DOTALL,
)


# ---------------------------------------------------------
# Skill model
# ---------------------------------------------------------
@dataclass
class Skill:
    """A single skill loaded from disk."""

    path: Path
    name: str
    description: str
    when_to_use: str
    enabled: bool
    body: str
    # "global" → ~/.vibe-cli/skills, "project" → ./.vibe/skills.
    # Project skills override global skills with the same name.
    scope: str = "global"

    @property
    def is_active(self) -> bool:
        """Whether this skill is enabled AND under the cap (visible to model)."""
        return self.enabled


# ---------------------------------------------------------
# Frontmatter parsing / serialisation
# ---------------------------------------------------------
def _parse_frontmatter_block(block: str) -> dict[str, str | bool]:
    """Tiny YAML-ish parser for the keys we actually use.

    We deliberately don't depend on PyYAML here — the frontmatter is a fixed,
    small schema (name, description, when_to_use, enabled), so a line-based
    `key: value` parser is enough and keeps skills.py self-contained.
    """
    out: dict[str, str | bool] = {}
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        if key == "enabled":
            out[key] = value.lower() in ("true", "yes", "on", "1")
        else:
            out[key] = value
    return out


def _parse_skill_file(path: Path) -> Skill | None:
    """Parse a single .md file into a Skill, or None if malformed."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    m = _FRONTMATTER_RE.match(text)
    if not m:
        # No frontmatter — treat the whole file as body, name from filename.
        body = text.strip()
        if not body:
            return None
        return Skill(
            path=path,
            name=path.stem,
            description="",
            when_to_use="",
            enabled=False,
            body=body,
        )

    fm = _parse_frontmatter_block(m.group("frontmatter"))
    body = m.group("body").strip()

    # Filename stem is the canonical id — frontmatter `name` is informational.
    name = path.stem
    return Skill(
        path=path,
        name=name,
        description=str(fm.get("description") or ""),
        when_to_use=str(fm.get("when_to_use") or ""),
        enabled=bool(fm.get("enabled", False)),
        body=body,
    )


def _serialize_skill(skill: Skill) -> str:
    """Render a Skill back to disk format.

    Keeps the same field order across writes so diffs are stable.
    """
    fm_lines = [
        "---",
        f"name: {skill.name}",
        f"description: {skill.description}",
    ]
    if skill.when_to_use:
        fm_lines.append(f"when_to_use: {skill.when_to_use}")
    fm_lines.append(f"enabled: {'true' if skill.enabled else 'false'}")
    fm_lines.append("---")
    return (
        "\n".join(fm_lines) + "\n" + (skill.body.rstrip() + "\n" if skill.body else "")
    )


def _atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` via a temp file + rename. Avoids torn writes
    that could leave a half-rewritten skill file on disk if the process is
    killed mid-toggle.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------
# Index
# ---------------------------------------------------------
@dataclass
class SkillsIndex:
    """In-memory snapshot of the skills directory.

    `active` is the (up to `max_active`) sorted list of enabled skills the
    model sees. `dropped` is the surplus enabled skills hidden from the
    model — surfaced to the user via /skills so they know to either bump
    the cap or disable some.
    """

    active: list[Skill] = field(default_factory=list)
    dropped: list[Skill] = field(default_factory=list)
    disabled: list[Skill] = field(default_factory=list)
    max_active: int = DEFAULT_MAX_ACTIVE

    @property
    def all(self) -> list[Skill]:
        """All skills on disk, alphabetically by name."""
        return sorted(
            self.active + self.dropped + self.disabled,
            key=lambda s: s.name,
        )

    @property
    def by_name(self) -> dict[str, Skill]:
        return {s.name: s for s in self.all}

    def get(self, name: str) -> Skill | None:
        return self.by_name.get(name)

    def is_visible_to_model(self, name: str) -> bool:
        """A skill is visible to the model only if it's in `active`."""
        return any(s.name == name for s in self.active)


# ---------------------------------------------------------
# Public API
# ---------------------------------------------------------
def _load_from_dir(directory: Path, scope: str) -> list[Skill]:
    """Parse every `*.md` in `directory`, tagging each with `scope`.

    Returns an empty list if the directory is absent or not a directory —
    project skills are optional, so a missing `.vibe/skills/` is normal.
    """
    if not directory.is_dir():
        return []
    parsed: list[Skill] = []
    for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_file() or entry.suffix.lower() != ".md":
            continue
        skill = _parse_skill_file(entry)
        if skill is not None:
            skill.scope = scope
            parsed.append(skill)
    return parsed


def load_skills(
    max_active: int = DEFAULT_MAX_ACTIVE,
    *,
    cwd: Path | str | None = None,
) -> SkillsIndex:
    """Scan SKILLS_DIR and `<cwd>/.vibe/skills/`, merge, apply the cap.

    Project skills override global skills with the same filename stem.
    Sort is by name; first `max_active` enabled skills become `active`,
    the rest land in `dropped`. Disabled skills go to `disabled`.
    """
    from src.logging_setup import emit_event, get_logger

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    global_skills = _load_from_dir(SKILLS_DIR, scope="global")
    project_skills = _load_from_dir(project_skills_dir(cwd), scope="project")

    # Project wins on name collision. Dict insertion order: global first,
    # then project values replace.
    merged: dict[str, Skill] = {s.name: s for s in global_skills}
    for s in project_skills:
        merged[s.name] = s
    parsed = list(merged.values())

    enabled = sorted([s for s in parsed if s.enabled], key=lambda s: s.name)
    disabled = sorted([s for s in parsed if not s.enabled], key=lambda s: s.name)

    if max_active is None or max_active < 0:
        max_active = DEFAULT_MAX_ACTIVE

    active = enabled[:max_active]
    dropped = enabled[max_active:]

    emit_event(
        get_logger("skill"),
        "skills.loaded",
        active=len(active),
        dropped=len(dropped),
        disabled=len(disabled),
        max_active=max_active,
        project_skills=len(project_skills),
        global_skills=len(global_skills),
    )

    return SkillsIndex(
        active=active,
        dropped=dropped,
        disabled=disabled,
        max_active=max_active,
    )


def set_enabled(
    name: str,
    enabled: bool,
    *,
    cwd: Path | str | None = None,
) -> Skill | None:
    """Flip a skill's `enabled` flag in its file. Returns the updated skill,
    or None if no such file exists.

    Resolution order matches the merged-index precedence: a project skill
    of the same name wins, so toggling shadowed names mutates the project
    copy, not the hidden global one.
    """
    from src.logging_setup import emit_event, get_logger

    project_path = project_skills_dir(cwd) / f"{name}.md"
    global_path = SKILLS_DIR / f"{name}.md"
    if project_path.is_file():
        path = project_path
        scope = "project"
    elif global_path.is_file():
        path = global_path
        scope = "global"
    else:
        return None

    skill = _parse_skill_file(path)
    if skill is None:
        return None
    skill.scope = scope
    if skill.enabled == enabled:
        return skill
    skill.enabled = enabled
    _atomic_write(path, _serialize_skill(skill))
    emit_event(
        get_logger("skill"),
        "skill.toggle",
        skill=name,
        enabled=enabled,
        scope=scope,
    )
    return skill


def seed_examples_if_empty(source_dir: Path) -> int:
    """Copy example skills into SKILLS_DIR on first run.

    Only seeds when the dir is empty (no .md files), so we never overwrite
    a user's local edits. Returns the number of files copied.
    """
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    existing = [p for p in SKILLS_DIR.iterdir() if p.suffix.lower() == ".md"]
    if existing:
        return 0
    if not source_dir.is_dir():
        return 0
    count = 0
    for src in source_dir.iterdir():
        if src.is_file() and src.suffix.lower() == ".md":
            shutil.copy2(src, SKILLS_DIR / src.name)
            count += 1
    return count


def render_skills_directory(skills: Iterable[Skill]) -> str:
    """Format the system-prompt block describing available skills.

    Empty list → empty string (caller skips the block entirely).
    """
    items = list(skills)
    if not items:
        return ""
    lines = [
        "You have these skills available. To use one, call the `run_skill`",
        "tool with its name; the tool will return the full instructions for",
        "the skill. Calling `run_skill` is a SETUP step — after the tool",
        "returns, you MUST write a user-facing reply that applies those",
        "instructions to the user's request. Do not stop after the tool call.",
        "",
    ]
    for s in items:
        lines.append(f"- {s.name} — {s.description or '(no description)'}")
        if s.when_to_use:
            lines.append(f"  When to use: {s.when_to_use}")
    return "\n".join(lines)
