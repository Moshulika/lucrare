"""Paste placeholder registry.

Large pastes (>`threshold` chars by default) are replaced in the prompt
with a short `[Paste #N <len> chars]` token so the input line stays
readable. On submit, `expand()` swaps placeholders back to the original
content so the LLM sees the full text.

The visible placeholder is wrapped in zero-width space guards (U+200B)
that the user cannot type from a keyboard. Substitution is done by
deterministic exact-string replace keyed on paste id — no parsing, no
regex on the hot path. A single cleanup regex strips any guard-wrapped
token that isn't in the registry (e.g. content the user pasted from
elsewhere that already contained guards).
"""

from __future__ import annotations

import re


# Defaults — overridden at startup from config.yml `ui.pastes`.
PASTE_THRESHOLD = 200
PASTE_MAX_PER_TURN = 5

# Zero-width space guards. Visually invisible, not typeable from a
# keyboard, so they reliably distinguish a real paste placeholder from a
# user-typed string that happens to look like one.
_GUARD = "​"

# Cleanup-only: matches any guard-wrapped placeholder token. Used after
# the deterministic replace loop to wipe placeholders that *aren't* in
# our registry (someone pasted them from outside the session).
_UNKNOWN_PLACEHOLDER_RE = re.compile(
    rf"{_GUARD}\[Paste #\d+ \d+ chars\]{_GUARD}"
)


def _placeholder_for(idx: int, length: int) -> str:
    return f"{_GUARD}[Paste #{idx} {length} chars]{_GUARD}"


class PasteRegistry:
    def __init__(
        self,
        *,
        threshold: int = PASTE_THRESHOLD,
        max_per_turn: int = PASTE_MAX_PER_TURN,
    ) -> None:
        self._pastes: dict[int, str] = {}
        self._counter = 0
        self.threshold = threshold
        self.max_per_turn = max_per_turn

    @property
    def at_capacity(self) -> bool:
        return len(self._pastes) >= self.max_per_turn

    def add(self, text: str) -> str | None:
        """Store `text` and return its guarded placeholder token.

        Returns None when the per-turn cap has been hit; the caller
        should fall back to inserting raw text in that case.
        """
        if self.at_capacity:
            return None
        self._counter += 1
        idx = self._counter
        self._pastes[idx] = text
        return _placeholder_for(idx, len(text))

    def expand(self, text: str) -> str:
        """Resolve placeholder tokens back to original content.

        - Each registered paste is replaced via exact string match
          (no regex on the hot path).
        - Any remaining guard-wrapped `[Paste #N N chars]` token is
          assumed to be foreign content and is dropped to empty string.
        - Stray solo guard chars are stripped so they don't reach the LLM.
        """
        for idx, original in self._pastes.items():
            text = text.replace(
                _placeholder_for(idx, len(original)), original, 1
            )
        text = _UNKNOWN_PLACEHOLDER_RE.sub("", text)
        return text.replace(_GUARD, "")

    def reset(self) -> None:
        self._pastes.clear()
        self._counter = 0


pastes = PasteRegistry()


def configure(threshold: int | None = None, max_per_turn: int | None = None) -> None:
    """Apply config-driven overrides to the global registry."""
    if threshold is not None:
        pastes.threshold = int(threshold)
    if max_per_turn is not None:
        pastes.max_per_turn = int(max_per_turn)
