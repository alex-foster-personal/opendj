"""The live-transport gate: nothing leaves for Sentry while a deck is playing.

The error-reporting policy (``(private path)``) says
"never send while any deck is playing or audible". This module is the code
that rule lives in. It answers one question, is the set live right now, from
two sources, and the answer decides whether an event is sent or stays in the
local sink. Nothing is dropped from the local JSONL or the daily client log;
those are written before this gate is consulted.

TWO SOURCES, ONE ANSWER

- A browser error carries ``any_deck_live`` straight from the page that owns
  the audio graph. That is the fresher, authoritative read and it wins when
  present.
- An engine exception has no such field, so the engine asks a registered
  PROBE. The engine entry point registers one that reads the page's UI
  mirror (``PUT /api/v1/state/ui-mirror``, published once a second) through
  :func:`mirror_transport_live`.

A probe that is missing, throws, or reads a stale mirror answers NOT LIVE,
and the event is sent. That direction is deliberate: the gate exists to keep
telemetry work off a live set, not to hide faults, and a broken probe that
silently dropped every event would be the worse failure. The staleness bound
is what stops a page that closed mid-track from muting the engine forever.

Stdlib only, no SDK: the gate has to be callable from the ``off`` build too.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping
from typing import Any

log = logging.getLogger(__name__)

LiveTransportProbe = Callable[[], bool]

#: The page republishes the mirror every 1 s and calls a 5 s gap a stall
#: (``mirror-publish-stall.ts``), so a mirror older than this is a page that
#: is gone or wedged, and its transport flags say nothing about now.
#: Inclusive: a mirror exactly this old is still current, one tick older is not.
MIRROR_LIVE_MAX_AGE_S: float = 15.0

#: Where ``PUT /api/v1/state/ui-mirror`` records WHEN the document arrived, on
#: the engine process's monotonic clock, beside the wall-clock ``received_at``.
#: Every in-process reader ages the mirror with this one (:func:`mirror_age_s`).
#: ``received_at`` cannot do that job: a wall clock steps (NTP, a manual change,
#: a resumed VM), and after a backward step a dead page's last document reads
#: as fresh for as long as the step was. The value means nothing outside the
#: process that wrote it, so ``GET /api/v1/state/ui-mirror`` does not serve it.
MIRROR_RECEIVED_MONOTONIC_KEY: str = "received_monotonic_s"


class _LiveGate:
    """Process-wide registry: the probe, and whether its failure was logged yet."""

    __slots__ = ("failed_logged", "probe")

    def __init__(self) -> None:
        self.probe: LiveTransportProbe | None = None
        self.failed_logged: bool = False


_GATE = _LiveGate()


def set_live_transport_probe(probe: LiveTransportProbe | None) -> None:
    """Register (or with ``None`` clear) the engine-side "is a deck live" read."""
    _GATE.probe = probe
    _GATE.failed_logged = False


def transport_is_live() -> bool:
    """True only when a registered probe positively says a deck is live."""
    if _GATE.probe is None:
        return False
    try:
        return bool(_GATE.probe())
    except Exception:  # any probe fault reads as "not live", by design
        if not _GATE.failed_logged:
            _GATE.failed_logged = True
            log.warning("live-transport probe raised; treating the set as not live", exc_info=True)
        return False


def mirror_age_s(
    mirror: Mapping[str, Any] | None,
    *,
    now_monotonic_s: float | None = None,
) -> float | None:
    """Seconds since the engine received this mirror, or ``None`` when unknown.

    Unknown means there is nothing to measure: no document, no
    ``MIRROR_RECEIVED_MONOTONIC_KEY`` stamp, a stamp that is not a finite
    number, or a stamp AHEAD of this process's clock (it was written by another
    process, whose monotonic origin is not ours). ``None`` is never "fresh" and
    never "stale"; each gate states which way it fails on it.
    """
    if mirror is None:
        return None
    stamp = mirror.get(MIRROR_RECEIVED_MONOTONIC_KEY)
    if isinstance(stamp, bool) or not isinstance(stamp, int | float) or not math.isfinite(stamp):
        return None
    current = time.monotonic() if now_monotonic_s is None else now_monotonic_s
    age_s = current - stamp
    return age_s if age_s >= 0 else None


def mirror_transport_live(
    mirror: Mapping[str, Any] | None,
    *,
    now_monotonic_s: float | None = None,
    max_age_s: float = MIRROR_LIVE_MAX_AGE_S,
) -> bool:
    """Read the UI mirror the page publishes: is any deck playing or audible?

    Both flags, not either alone, for the same reason the page's own
    ``anyDeckPlaying`` reads both: ``playing`` leads ``audible`` by the
    schedule lead and ``audible`` outlives a stop that is still ringing out.

    A mirror of unknown age (:func:`mirror_age_s`) or one older than
    ``max_age_s`` is NOT live: a closed page publishes nothing, and its last
    document must not keep muting the engine.
    """
    age_s = mirror_age_s(mirror, now_monotonic_s=now_monotonic_s)
    if mirror is None or age_s is None or age_s > max_age_s:
        return False
    decks = mirror.get("decks")
    if not isinstance(decks, Mapping):
        return False
    for deck in decks.values():
        if not isinstance(deck, Mapping):
            continue
        if deck.get("playing") is True or deck.get("audible") is True:
            return True
    return False


__all__ = [
    "MIRROR_LIVE_MAX_AGE_S",
    "MIRROR_RECEIVED_MONOTONIC_KEY",
    "LiveTransportProbe",
    "mirror_age_s",
    "mirror_transport_live",
    "set_live_transport_probe",
    "transport_is_live",
]
