"""Runtime gates that defer CloudSync while Gig posture or a playing deck is active."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from apps.shared.app_posture import AppPosture, read_posture_from_prefs

DEFER_REASON_GIG = "gig_posture"
DEFER_REASON_DECK_PLAYING = "deck_playing"
DEFER_REASON_PRESSURE_SHED = "pressure_shed"

#: Another process already holds this data dir's cross-process sync flock
#: (``apps.sync_hub.single_flight.sync_flock_for``) -- a scheduler round, a
#: Sync now, or another CLI invocation is already mid-round against the same
#: ``state.db``. Distinct from the three reasons above: those refuse because
#: it is UNSAFE to sync right now (a deck may be live); this one refuses
#: because a round is already IN FLIGHT, force or not.
DEFER_REASON_SYNC_IN_PROGRESS = "sync_in_progress"


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
    return any(
        isinstance(deck, dict) and deck.get("playing") is True
        for deck in decks.values()
    )


def session_xruns_elevated(ui_mirror: Mapping[str, Any] | None) -> bool:
    """True when mirror reports xrun_sentinel.xruns > 0."""
    if ui_mirror is None:
        return False
    sentinel = ui_mirror.get("xrun_sentinel")
    if not isinstance(sentinel, dict):
        return False
    xruns = sentinel.get("xruns")
    return isinstance(xruns, int) and xruns > 0


def read_pressure_elevated(
    payload: Mapping[str, Any] | None = None,
    *,
    reader: Callable[[], Mapping[str, Any]] | None = None,
) -> bool:
    """True when a supplied or reader-provided pressure payload is elevated."""
    from apps.shared.machine_pressure_signal import pressure_is_elevated

    if payload is not None:
        return pressure_is_elevated(payload)
    if reader is not None:
        return pressure_is_elevated(reader())
    return False


def refuse_sync_round(
    data_dir: Path,
    ui_mirror: Mapping[str, Any] | None,
    *,
    force: bool = False,
    pressure_payload: Mapping[str, Any] | None = None,
) -> str | None:
    """Return a defer reason string, or None when sync may proceed."""
    if force:
        return None
    if read_posture_from_prefs(data_dir) == AppPosture.GIG:
        return DEFER_REASON_GIG
    if any_deck_playing(ui_mirror):
        if read_pressure_elevated(pressure_payload) or session_xruns_elevated(ui_mirror):
            return DEFER_REASON_PRESSURE_SHED
        return DEFER_REASON_DECK_PLAYING
    return None


__all__ = [
    "DEFER_REASON_DECK_PLAYING",
    "DEFER_REASON_GIG",
    "DEFER_REASON_PRESSURE_SHED",
    "DEFER_REASON_SYNC_IN_PROGRESS",
    "SyncDeferredError",
    "any_deck_playing",
    "read_pressure_elevated",
    "refuse_sync_round",
    "session_xruns_elevated",
]
