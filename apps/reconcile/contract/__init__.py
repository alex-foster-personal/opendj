"""Public contract of ``apps.reconcile`` (ADR-NEW-module-contracts, M1).

Code outside ``apps.reconcile`` reaches the package only through this module
(and, for delivery layers, ``apps.reconcile.http``). Pure: stdlib, pydantic
and typing only.

``Candidate`` and ``VETO_SIGNALS`` were MOVED here unchanged from
``apps.reconcile.locate``, which re-exports them, so the locate pipeline and
the relocate route keep handling the very same objects. ``Candidate`` is now
frozen, as every contract type must be; nothing assigned to its fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

CONTRACT_VERSION = "reconcile/2"  # /2: Candidate is a frozen dataclass and its signals a tuple

# Signals that rule a candidate out of automatic apply, whatever else fired.
VETO_SIGNALS: frozenset[str] = frozenset({"fingerprint_mismatch"})


@dataclass(slots=True, frozen=True)
class Candidate:
    """A scored match for a single broken RB row."""

    path: Path
    confidence: float = 0.0
    signals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Callers build the signals as a list; the frozen candidate keeps them as a tuple.
        object.__setattr__(self, "signals", tuple(self.signals))

    @property
    def signal_count(self) -> int:
        return len(self.signals)

    @property
    def vetoed(self) -> bool:
        return any(s in VETO_SIGNALS for s in self.signals)

    @property
    def triple_validated(self) -> bool:
        if self.vetoed:
            return False
        return sum(1 for s in self.signals if s not in VETO_SIGNALS) >= 3


class RelocateQuery(BaseModel):
    """One track to find on disk: the row shape ``locate`` scores against.

    Every field is the string ``locate`` reads from a ``broken.csv`` row;
    an unknown value is the empty string, as in that csv.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    original_path: str
    basename: str
    title: str = ""
    artist: str = ""
    duration_s: str = ""
    file_size: str = ""
    stable_id: str = ""

    def as_row(self) -> dict[str, str]:
        """The ``dict[str, str]`` row ``apps.reconcile.locate`` consumes."""
        return {
            "original_path": self.original_path,
            "basename": self.basename,
            "title": self.title,
            "artist": self.artist,
            "duration_s": self.duration_s,
            "file_size": self.file_size,
            "stable_id": self.stable_id,
        }


@runtime_checkable
class ReconcileService(Protocol):
    """The operations code outside ``apps.reconcile`` uses today."""

    def find_relocate_candidates(self, query: RelocateQuery, *, limit: int) -> list[Candidate]:
        """Ranked on-disk candidates for ``query`` (best first), at most ``limit``.

        Scans the music roots, builds the filesystem index, opens the
        duplicate scan's fingerprint evidence when one exists, and scores
        with ``apps.reconcile.locate``. Read-only.
        """
        ...


__all__ = [
    "CONTRACT_VERSION",
    "VETO_SIGNALS",
    "Candidate",
    "ReconcileService",
    "RelocateQuery",
]
