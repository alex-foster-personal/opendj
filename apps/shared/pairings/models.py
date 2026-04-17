"""Frozen dataclass for one ``pairings`` row.

Kept separate from :mod:`apps.shared.pairings.repo` so the model can be
imported by CLI / serialiser code without pulling in ``sqlite3``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Direction = Literal["into", "out_of", "either"]
Source = Literal["manual", "learned", "ai"]

DIRECTIONS: tuple[str, ...] = ("into", "out_of", "either")
SOURCES: tuple[str, ...] = ("manual", "learned", "ai")


@dataclass(frozen=True)
class PairingEdge:
    """One row from the ``pairings`` table.

    Timestamps are parsed to :class:`datetime` objects on read; callers
    that need the raw ISO string can format via ``.isoformat()``.
    """

    from_stable_id: str
    to_stable_id: str
    direction: str           # one of DIRECTIONS
    source: str              # one of SOURCES
    notes: str | None
    confidence: float | None
    created_at: datetime
    modified_at: datetime

    def as_dict(self) -> dict[str, object]:
        """Serialise to a plain JSON-friendly dict.

        Used by the ``--format json`` CLI and (later) by the open-dj
        exporter in Phase 15.
        """
        return {
            "from_stable_id": self.from_stable_id,
            "to_stable_id": self.to_stable_id,
            "direction": self.direction,
            "source": self.source,
            "notes": self.notes,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat(),
            "modified_at": self.modified_at.isoformat(),
        }


__all__ = ["Direction", "Source", "DIRECTIONS", "SOURCES", "PairingEdge"]
