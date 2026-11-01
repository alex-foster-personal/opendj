"""Shared helpers for reading downbeats from beatgrid beat lists."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def downbeats_from_beats(beats: Iterable[Any]) -> list[float]:
    """Downbeat times (``n == 1``) from a beatgrid ``beats`` sequence."""
    return [float(b["t"]) for b in beats if int(b.get("n", 0)) == 1]
