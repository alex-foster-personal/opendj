"""Shared validation for Mixed In Key's 1-9 library-display scale."""

from __future__ import annotations


def readable_mik_energy(value: object) -> int | None:
    """Return a whole MIK display value in the inclusive 1-9 range."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not float(value).is_integer():
        return None
    energy = int(value)
    return energy if 1 <= energy <= 9 else None
