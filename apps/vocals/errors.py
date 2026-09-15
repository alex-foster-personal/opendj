"""Typed errors for vocals/stems library code (daemon-safe; no SystemExit)."""

from __future__ import annotations


class UnknownPlaylistError(Exception):
    """Raised when a named playlist is absent from state.db."""

    def __init__(self, name: str, known: list[str]) -> None:
        self.name = name
        self.known = known
        known_text = ", ".join(known) or "(none)"
        super().__init__(f"error: unknown playlist {name!r}. Known: {known_text}")
