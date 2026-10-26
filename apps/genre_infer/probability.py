"""Shared parsing helpers for genre-inference JSON payloads."""

from __future__ import annotations

from typing import Any


def unit_probability(value: Any) -> float | None:
    """A probability in [0, 1], or None.

    Compared before float() so a huge JSON integer is rejected instead of
    overflowing; JSON true/false are ints in Python, and a boolean is not a
    probability.
    """
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        return None
    return float(value)


__all__ = ["unit_probability"]
