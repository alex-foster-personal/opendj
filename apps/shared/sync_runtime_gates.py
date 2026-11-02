"""Runtime gates that defer CloudSync while Gig posture or a playing deck is active.

THE MIRROR HAS AN AGE, AND THE GATES READ IT

Both mirror gates below read the document the performance page publishes once
a second. A page that is killed mid-track sends no DELETE, so its last
"playing" document stays on the engine until the process exits. Read without an
age bound, that document deferred every scheduler round and refused every
unforced Sync now with ``deck_playing`` until the engine restarted.

So a mirror whose age is KNOWN and past ``MIRROR_LIVE_MAX_AGE_S`` (the bound the
telemetry live-set gate already uses, reused rather than restated) describes a
page that is gone, and none of its flags say anything about now. The age comes
from the engine's monotonic arrival stamp, never from wall-clock
``received_at``: see ``apps.shared.telemetry.live.MIRROR_RECEIVED_MONOTONIC_KEY``.

A mirror of UNKNOWN age is still trusted. That is the opposite of what the
telemetry gate does with the same input, on purpose: each gate fails toward
its own cheap mistake. Wrongly sending one telemetry event costs nothing;
wrongly starting a sync round puts SQLite write transactions and hub I/O under
a deck that may be live. A refused round is loud (the journal says
``deferred: deck_playing``) and Force sync overrides it.

WHAT THE BOUND CANNOT TELL APART

A stale mirror means the page stopped PUBLISHING, which is not the same as the
audio stopping. A page whose main thread is wedged while its audio thread keeps
rendering goes stale too (issue #2154 recorded 17 s of that), and so does a
healthy ``odj-audio`` process whose renderer died: in Rust engine mode the page
only relays the engine's play state. Both release this gate after the bound
with a deck still audible. Gig posture refuses the round regardless; closing
the gap properly needs a play-state source that does not go through the page.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from apps.shared.app_posture import AppPosture, read_posture_from_prefs
from apps.shared.telemetry import MIRROR_LIVE_MAX_AGE_S, mirror_age_s

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


def mirror_is_stale(ui_mirror: Mapping[str, Any], now_monotonic_s: float | None) -> bool:
    """True only when the mirror's age is KNOWN and past the bound (inclusive bound: equal is current)."""
    age_s = mirror_age_s(ui_mirror, now_monotonic_s=now_monotonic_s)
    return age_s is not None and age_s > MIRROR_LIVE_MAX_AGE_S


def any_deck_playing(
    ui_mirror: Mapping[str, Any] | None,
    *,
    now_monotonic_s: float | None = None,
) -> bool:
    """True when a current ui_mirror shows at least one deck with playing=True.

    A stale mirror (see the module docstring) answers False whatever it says.
    ``now_monotonic_s`` is the engine's monotonic clock; omit it outside tests.
    """
    if ui_mirror is None or mirror_is_stale(ui_mirror, now_monotonic_s):
        return False
    decks = ui_mirror.get("decks")
    if not isinstance(decks, dict):
        return False
    return any(
        isinstance(deck, dict) and deck.get("playing") is True
        for deck in decks.values()
    )


def session_xruns_elevated(
    ui_mirror: Mapping[str, Any] | None,
    *,
    now_monotonic_s: float | None = None,
) -> bool:
    """True when a current mirror reports xrun_sentinel.xruns > 0 (stale answers False)."""
    if ui_mirror is None or mirror_is_stale(ui_mirror, now_monotonic_s):
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
    now_monotonic_s: float | None = None,
) -> str | None:
    """Return a defer reason string, or None when sync may proceed."""
    if force:
        return None
    if read_posture_from_prefs(data_dir) == AppPosture.GIG:
        return DEFER_REASON_GIG
    if any_deck_playing(ui_mirror, now_monotonic_s=now_monotonic_s):
        if read_pressure_elevated(pressure_payload) or session_xruns_elevated(
            ui_mirror, now_monotonic_s=now_monotonic_s
        ):
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
    "mirror_is_stale",
    "read_pressure_elevated",
    "refuse_sync_round",
    "session_xruns_elevated",
]
