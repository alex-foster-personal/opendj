"""Runtime gates that defer CloudSync while Gig posture or a playing deck is active."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from apps.shared.app_posture import AppPosture, read_posture_from_prefs

DEFER_REASON_GIG = "gig_posture"
DEFER_REASON_DECK_PLAYING = "deck_playing"


class SyncDeferredError(Exception):
    """A sync round was refused before any hub I/O."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def any_deck_playing(ui_mirror: Mapping[str, Any] | None) -> bool:
    """True when the latest ui_mirror shows at least one deck with playing=True."""
    if ui_mirror is None:
        return False
    decks = ui_mirror.get("decks")
    if not isinstance(decks, dict):
        return False
    for deck in decks.values():
        if isinstance(deck, dict) and deck.get("playing") is True:
            return True
    return False


def refuse_sync_round(
    data_dir: Path,
    ui_mirror: Mapping[str, Any] | None,
    *,
    force: bool = False,
) -> str | None:
    """Return a defer reason string, or None when sync may proceed."""
    if force:
        return None
    if read_posture_from_prefs(data_dir) == AppPosture.GIG:
        return DEFER_REASON_GIG
    if any_deck_playing(ui_mirror):
        return DEFER_REASON_DECK_PLAYING
    return None


__all__ = [
    "DEFER_REASON_DECK_PLAYING",
    "DEFER_REASON_GIG",
    "SyncDeferredError",
    "any_deck_playing",
    "refuse_sync_round",
]
