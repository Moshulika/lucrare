"""Seed and reconcile user-side YAML config files.

On first run we copy the bundled defaults (e.g. ``config/config.yml``) into
``~/.vibe-cli/`` so users have a real file to edit. On every subsequent
startup we walk the bundled defaults and insert any missing keys into the
user file — preserving the user's existing values, extra keys, comments,
and formatting via ``ruamel.yaml``'s round-trip loader.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _merge_missing(default, user) -> bool:
    """Insert keys present in ``default`` but missing in ``user``.

    Only descends into nested mappings. Lists and scalars are taken from
    ``user`` whenever they exist there, so user customisations (model
    lists, thresholds, etc.) are never overwritten. Returns ``True`` if
    any change was made.
    """
    if not isinstance(default, CommentedMap) or not isinstance(user, CommentedMap):
        return False
    changed = False
    for key in default.keys():
        if key not in user:
            user[key] = default[key]
            changed = True
            continue
        if _merge_missing(default[key], user[key]):
            changed = True
    return changed


def ensure_user_config(default_path: Path, user_path: Path) -> str:
    """Make sure ``user_path`` exists and contains every default key.

    Returns one of ``"seeded"`` (file was just created), ``"updated"``
    (existing file gained one or more missing keys), or ``"unchanged"``.
    """
    user_path.parent.mkdir(parents=True, exist_ok=True)

    if not user_path.exists():
        shutil.copy2(default_path, user_path)
        return "seeded"

    yaml = _yaml()
    with default_path.open("r", encoding="utf-8") as f:
        default_data = yaml.load(f)
    with user_path.open("r", encoding="utf-8") as f:
        user_data = yaml.load(f)

    if default_data is None or user_data is None:
        return "unchanged"

    if _merge_missing(default_data, user_data):
        with user_path.open("w", encoding="utf-8") as f:
            yaml.dump(user_data, f)
        return "updated"

    return "unchanged"