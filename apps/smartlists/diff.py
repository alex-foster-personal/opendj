"""Set-diff helper for materialisation."""
from __future__ import annotations


def diff_sets(old: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    """Return ``(added, removed)`` preserving order from ``new`` / ``old``."""
    old_seen = set(old)
    new_seen = set(new)
    added = [sid for sid in new if sid not in old_seen]
    removed = [sid for sid in old if sid not in new_seen]
    return added, removed


__all__ = ["diff_sets"]
