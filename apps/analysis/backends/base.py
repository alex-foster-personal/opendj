"""Analyser backend protocol + shared exceptions."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..record import AnalysisRecord


class BackendNotAvailable(RuntimeError):
    """Backend runtime dep missing (e.g. ``mixed-in-key-cli`` not on PATH)."""


class TrackTooLong(RuntimeError):
    """Track exceeded ``analyzer.max_track_minutes``."""


class TrackUnreadable(RuntimeError):
    """THIS FILE could not be read or decoded.

    The one failure a backend can have that is a fact about the input rather
    than about the machine, so it is the one a chunking caller may keep going
    past. A backend raises it where it knows the file is the problem - a
    decode that failed, an empty stream, a converter that rejected it - and
    lets everything else propagate, because a missing config, a broken model
    download or a dead worker pool will meet the next file exactly the same
    way and the caller needs to be told to stop.
    """


class TrackVanished(RuntimeError):
    """THIS FILE was gone by the time the backend opened it.

    Deliberately not a subclass of :class:`TrackUnreadable`, because the
    caller has to do something different with it. An unreadable file is here
    and really was attempted, so recording the queue as tried is honest. A
    file that disappeared between the caller's admission check and the decode
    was never attempted at all, and if it is restored byte-identically its
    content token is unchanged - so a queue booked as tried would leave that
    track sitting behind an `unchanged` verdict forever, never analyzed.

    The window is small but real: the drain checks ``exists()`` when it
    builds the handoff and the backend opens the file some chunks later,
    with a library sync, a rename or an unmount free to happen in between.
    """


@runtime_checkable
class AnalyzerBackend(Protocol):
    name: str
    version: str

    @classmethod
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:
        """Pure compute; must not write to disk or state layer."""
        ...  # pragma: no cover


__all__ = [
    "AnalyzerBackend",
    "BackendNotAvailable",
    "TrackTooLong",
    "TrackUnreadable",
    "TrackVanished",
]
