"""Public contract for tonight's tracklist (NOWP, issue #10341).

Code outside ``apps.now_playing`` imports only this module; delivery layers may also use
``apps.now_playing.http``. It depends on the standard library only.
"""

from __future__ import annotations

from typing import Protocol

CONTRACT_VERSION = "now_playing/1"

#: Text for a play whose track is gone from the library (null metadata).
UNKNOWN_LABEL = "Unknown"


class TrackLike(Protocol):
    """The library metadata a tracklist row joins onto a play."""

    artist: str | None
    title: str | None
    key: str | None
    bpm: float | None


__all__ = ["CONTRACT_VERSION", "UNKNOWN_LABEL", "TrackLike"]
