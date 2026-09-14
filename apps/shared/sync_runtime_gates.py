"""Runtime gates that defer CloudSync while Gig posture or a playing deck is active."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from apps.shared.app_posture import AppPosture, read_posture_from_prefs

DEFER_REASON_GIG = "gig_posture"
DEFER_REASON_DECK_PLAYING = "deck_playing"
DEFER_REASON_PRESSURE_SHED = "pressure_shed"


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
    """Adapter over machine_pressure.pressure_is_elevated; inject reader in tests."""
    if payload is not None:
        return _pressure_is_elevated_from_payload(payload)
    if reader is not None:
        return _pressure_is_elevated_from_payload(reader())
    try:
        from apps.webui.server.machine_pressure import pressure_is_elevated, read_machine_pressure

        return pressure_is_elevated(read_machine_pressure())
    except ImportError:
        return False


def _pressure_is_elevated_from_payload(payload: Mapping[str, Any]) -> bool:
    try:
        from apps.webui.server.machine_pressure import pressure_is_elevated

        return pressure_is_elevated(payload)
    except ImportError:
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
    "SyncDeferredError",
    "any_deck_playing",
    "read_pressure_elevated",
    "refuse_sync_round",
    "session_xruns_elevated",
]
