"""Open DJ own-deck source -- records what WE played, not what another app did.

Why this source is push-fed and the other two are pull-fed
----------------------------------------------------------

``djay_source`` and ``rb_source`` poll another application's database.
Open DJ has no such database to poll: its decks are a browser Web Audio
graph (``apps/webui/frontend/src/lib/rb/audio-engine.svelte.ts``), the
Python side never decodes or plays a sample, and the only server-visible
trace of a deck load is an anonymous ``GET /api/v1/tracks/{id}/audio``
that proves a load was ATTEMPTED and never that it was heard. So the
authoritative deck state lives in the browser and has to be pushed here.

The wire unit is therefore a periodic SNAPSHOT of deck state, not a
stream of play/pause transitions. Snapshots are chosen deliberately:
a snapshot carries the dwell accumulated so far, so a browser that
reloads, crashes, or is closed mid-track still leaves every second it
already reported banked in sqlite. A transition stream would lose an
entire play whenever the closing "pause" never arrived.

The class still exposes ``poll_once()`` so it plugs into
:meth:`apps.sets.record.Recorder.attach_source` unchanged: the recorder's
existing poll thread drains whatever the HTTP ingest enqueued.

What counts as "played" -- RECORD THE VALUE, FILTER AT READ TIME
---------------------------------------------------------------

Neither sibling source applies a dwell threshold. Both delegate the
"was this actually played" judgement to the upstream app: Rekordbox
decides what earns a HISTORY row (and writes it on track END), djay
decides what earns a history item. Each source then emits one
``track_loaded`` per row it had not seen before.

For Open DJ we ARE the upstream, so the judgement would be ours -- and
this source deliberately declines to make it at capture time.

``docs/product/set-dwell-threshold-analysis.md`` measured 3678 real
dwell gaps across 127 rekordbox sessions and found the distribution has
**no trough**: a flat, thin shelf below 60s that climbs monotonically
into a broad mode at 120-240s, with every sub-60s bucket inside about
1.5 sigma of every other. The two populations a threshold is meant to
separate -- "auditioned and dropped" versus "actually played" -- are not
separated in the data, so no value of X is defensible.

The earlier 30s capture-time gate was measured to silently delete at
least one track from **44 of 82 sessions (54%)**, with no record that
anything had been removed, because the gate decided whether the row was
written at all. That is now gone.

So: a play row is written as soon as a track accrues ANY audible time,
and the row carries ``audible_s`` and ``played_fraction`` as VALUES.
Deciding what counts as played is a read-time filter (see
:func:`apps.play_analytics.query.query_play_analytics`), which is
reversible, auditable, adjustable per set, and re-runnable months later
against a set already recorded. A capture-time gate is none of those.

Audible, not ``playing``: a deck can be in the playing transport state
while every stem is muted or its channel fader is down, and a track
nobody heard is not a track that was played. A track that is loaded and
never becomes audible still writes nothing -- that is the one judgement
made here, and it is a fact about the audio, not a taste threshold.

:data:`MAX_SNAPSHOT_GAP_S` bounds how much a single gap between two
snapshots may contribute, so a backgrounded tab whose timers were
throttled for an hour cannot bank an hour of imaginary playback.

Fail-fast boundary
------------------

:meth:`OpenDjDeckSource.submit` validates hard and raises
:class:`DeckObservationError` on anything it cannot interpret -- an
unknown deck, a missing field, a truthy-but-not-bool transport flag, a
timestamp that is not explicitly UTC, an impossible state such as
audible-with-no-track. Bad input is rejected loudly at the door (the
HTTP layer turns it into a 422) rather than being swallowed into a log
line, because a silently dropped observation is an under-counted set.

``poll_once`` deliberately guards nothing. Everything it touches was
already validated by ``submit``, and
:meth:`apps.sets.record.Recorder._poll_loop` already turns a raising
source into a ``source_error`` event, so a second guard here would only
hide the failure one frame earlier than the machinery built to report
it. That is a divergence from the sibling sources, which each carry
their own blind ``except Exception``; theirs predates nothing in
particular and duplicates the recorder's guard.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..state import Event, SetsState
from .opendj_wire import (
    KNOWN_DECK_IDS,
    DeckObservation,
    DeckObservationError,
    DeckSnapshot,
    iso_utc,
    parse_snapshot,
    played_fraction,
)

logger = logging.getLogger(__name__)

SOURCE_NAME = "opendj_decks"

#: Advisory read-time filter default, in seconds of audible playback.
#: This is NOT a capture gate -- nothing here refuses to write a row
#: because of it. It is stamped into each play row as ``threshold_s`` so
#: a set records which filter default was current when it was captured.
#:
#: 60s rather than the original 30s on the measured evidence in
#: docs/product/set-dwell-threshold-analysis.md: it is where the flat
#: sub-60s shelf gives way to the mix mode, and it sits below p10 (77s)
#: so it stays clear of the main distribution. It is a shoulder, not a
#: trough, so it is an adjustable default and never a calibrated constant.
ADVISORY_DWELL_S: float = 60.0

#: The emitter's target snapshot cadence. Published so the browser
#: emitter and any agent driving the HTTP ingest agree on one number.
SNAPSHOT_INTERVAL_S: float = 1.0

#: Most seconds a single inter-snapshot gap may contribute to dwell.
#: A larger gap means we lost observations and cannot honestly claim
#: the deck stayed audible across it.
MAX_SNAPSHOT_GAP_S: float = 5.0


@dataclass
class _DeckAccumulator:
    """Per-deck running dwell for the track currently loaded on it."""

    stable_id: str | None = None
    audible_s: float = 0.0
    #: Row id of this generation's play row, once it has been written.
    #: None means the track has not yet been audible for a single second.
    event_id: int | None = None
    last_observed_at: datetime | None = None
    last_audible: bool = False
    last_playing: bool = False
    last_position_ms: float = 0.0
    duration_ms: float | None = None
    title: str | None = None
    artist: str | None = None

    def reset_for(self, obs: DeckObservation, at: datetime) -> None:
        """Start a fresh dwell generation for a newly loaded track."""
        self.stable_id = obs.stable_id
        self.audible_s = 0.0
        self.event_id = None
        self.last_observed_at = at
        self.last_audible = obs.audible
        self.last_playing = obs.playing
        self.last_position_ms = obs.position_ms
        self.duration_ms = obs.duration_ms
        self.title = obs.title
        self.artist = obs.artist


# ---------------------------------------------------------------------------
# source
# ---------------------------------------------------------------------------


class OpenDjDeckSource:
    """Turns pushed Open DJ deck snapshots into set-timeline rows.

    Emits the same event shape as the sibling sources::

        action = "track_loaded"  -- this track became audible; the row's
                                    audible_s is kept current as it plays
        action = "track_change"  -- the audible baton moved to another deck

    One instance per live recorder session.
    """

    def __init__(
        self,
        session_id: str,
        state: SetsState,
        *,
        session_started_at: datetime,
        on_event: Callable[[Event], None] | None = None,
        advisory_dwell_s: float = ADVISORY_DWELL_S,
        max_snapshot_gap_s: float = MAX_SNAPSHOT_GAP_S,
    ) -> None:
        if advisory_dwell_s <= 0:
            raise ValueError("advisory_dwell_s must be positive")
        if max_snapshot_gap_s <= 0:
            raise ValueError("max_snapshot_gap_s must be positive")
        self.session_id = session_id
        self.state = state
        self.session_started_at = session_started_at
        self.advisory_dwell_s = advisory_dwell_s
        self.max_snapshot_gap_s = max_snapshot_gap_s
        self._on_event_cb = on_event
        self._lock = threading.Lock()
        self._pending: list[DeckSnapshot] = []
        self._decks: dict[str, _DeckAccumulator] = {}
        self._last_submitted_at: datetime | None = None
        self._last_recorded_deck: str | None = None
        self._last_recorded_stable_id: str | None = None
        self._accepted = 0

    # ------------------------------------------------------------------
    # ingest (HTTP thread)
    # ------------------------------------------------------------------

    def submit(self, payload: Any) -> DeckSnapshot:
        """Validate and enqueue one snapshot. Raises on anything odd."""
        snapshot = parse_snapshot(payload)
        with self._lock:
            if (
                self._last_submitted_at is not None
                and snapshot.observed_at < self._last_submitted_at
            ):
                raise DeckObservationError(
                    f"snapshot at {snapshot.observed_at.isoformat()} is older "
                    f"than the last one at {self._last_submitted_at.isoformat()}"
                )
            self._last_submitted_at = snapshot.observed_at
            self._pending.append(snapshot)
            self._accepted += 1
        return snapshot

    def submit_many(self, payloads: list[Any]) -> int:
        """Validate and enqueue a batch, in order. Raises on the first bad one."""
        for payload in payloads:
            self.submit(payload)
        return len(payloads)

    # ------------------------------------------------------------------
    # drain (recorder poll thread)
    # ------------------------------------------------------------------

    def poll_once(self) -> None:
        """Fold every queued snapshot into dwell and emit any new rows.

        Deliberately catches nothing. Every snapshot in the queue was
        already validated by :meth:`submit`, so the only thing left that
        can fail here is the sqlite write -- and
        :meth:`apps.sets.record.Recorder._poll_loop` already wraps each
        source's ``poll_once`` and turns a raise into a ``source_error``
        event. A second guard here would swallow the failure one frame
        earlier and hide it from that machinery.
        """
        with self._lock:
            batch = self._pending
            self._pending = []
        for snapshot in batch:
            self._apply(snapshot)

    def _apply(self, snapshot: DeckSnapshot) -> None:
        for obs in snapshot.decks:
            acc = self._decks.get(obs.deck)
            if acc is None:
                acc = _DeckAccumulator()
                self._decks[obs.deck] = acc
                acc.reset_for(obs, snapshot.observed_at)
                continue
            if obs.stable_id != acc.stable_id:
                # A different track now occupies the deck: new generation.
                acc.reset_for(obs, snapshot.observed_at)
                continue
            self._credit(acc, obs, snapshot.observed_at)
            if acc.audible_s <= 0:
                # Loaded but never yet audible. Nothing was heard, so there
                # is nothing to record; this is the only judgement made at
                # capture time, and it is a fact about the audio rather than
                # a taste threshold.
                continue
            if acc.event_id is None:
                self._open_play_row(obs, acc, snapshot.observed_at)
            else:
                self.state.update_event_value(acc.event_id, self._play_value(acc, obs))

    def _credit(
        self,
        acc: _DeckAccumulator,
        obs: DeckObservation,
        at: datetime,
    ) -> None:
        """Bank the interval since the previous observation of this deck."""
        if acc.last_observed_at is not None and acc.last_audible:
            elapsed = (at - acc.last_observed_at).total_seconds()
            if elapsed > 0:
                acc.audible_s += min(elapsed, self.max_snapshot_gap_s)
        acc.last_observed_at = at
        acc.last_audible = obs.audible
        acc.last_playing = obs.playing
        acc.last_position_ms = obs.position_ms
        # Metadata can arrive after the first sighting of a track.
        if obs.duration_ms is not None:
            acc.duration_ms = obs.duration_ms
        if obs.title is not None:
            acc.title = obs.title
        if obs.artist is not None:
            acc.artist = obs.artist

    # ------------------------------------------------------------------
    # emission
    # ------------------------------------------------------------------

    def _play_value(
        self,
        acc: _DeckAccumulator,
        obs: DeckObservation,
    ) -> dict[str, Any]:
        """The play row's payload, refreshed as dwell accumulates.

        ``played_fraction`` is carried alongside ``audible_s`` because
        seconds alone mislead across a library spanning 91s edits to
        63-minute recorded mixes: 60s is most of a short edit and a
        rounding error on a long one.
        """
        return {
            "title": acc.title,
            "artist": acc.artist,
            "duration_ms": acc.duration_ms,
            "position_ms": obs.position_ms,
            "audible_s": round(acc.audible_s, 3),
            "played_fraction": played_fraction(acc.audible_s, acc.duration_ms),
            "threshold_s": self.advisory_dwell_s,
            "note": (
                "Open DJ own deck. Row written on first audible second; "
                "audible_s/played_fraction are VALUES, not a gate. Filter "
                "at read time -- see docs/product/"
                "set-dwell-threshold-analysis.md"
            ),
        }

    def _open_play_row(
        self,
        obs: DeckObservation,
        acc: _DeckAccumulator,
        at: datetime,
    ) -> None:
        """Write this generation's play row the moment it is first audible."""
        rel = self._rel_ts(at)
        wall = iso_utc(at)
        event = Event(
            session_id=self.session_id,
            timestamp_s=rel,
            wall_clock=wall,
            deck=obs.deck,
            track_stable_id=obs.stable_id,
            action="track_loaded",
            source=SOURCE_NAME,
            value=self._play_value(acc, obs),
        )
        acc.event_id = self.state.record_event(event)
        if self._on_event_cb is not None:
            self._on_event_cb(event)
        if (
            self._last_recorded_deck is not None
            and self._last_recorded_deck != obs.deck
        ):
            self._emit(
                Event(
                    session_id=self.session_id,
                    timestamp_s=rel,
                    wall_clock=wall,
                    deck=obs.deck,
                    track_stable_id=obs.stable_id,
                    action="track_change",
                    source=SOURCE_NAME,
                    value={
                        "from_deck": self._last_recorded_deck,
                        "to_deck": obs.deck,
                        "from_stable_id": self._last_recorded_stable_id,
                        "to_stable_id": obs.stable_id,
                    },
                )
            )
        self._last_recorded_deck = obs.deck
        self._last_recorded_stable_id = obs.stable_id

    def _emit(self, event: Event) -> None:
        """Write the row, then notify the optional listener.

        Both calls are allowed to raise. A dropped row is an
        under-counted set, which is precisely the failure this whole
        source exists to prevent, so it surfaces as a ``source_error``
        on the timeline rather than as a log line nobody reads.
        """
        self.state.record_event(event)
        if self._on_event_cb is not None:
            self._on_event_cb(event)

    def _rel_ts(self, at: datetime) -> float:
        return max(0.0, (at - self.session_started_at).total_seconds())

    # ------------------------------------------------------------------
    # agent-native read surface
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Everything an agent needs to see why a row did or did not appear."""
        with self._lock:
            pending = len(self._pending)
            accepted = self._accepted
        return {
            "source": SOURCE_NAME,
            "session_id": self.session_id,
            "advisory_dwell_s": self.advisory_dwell_s,
            "gates_capture": False,
            "max_snapshot_gap_s": self.max_snapshot_gap_s,
            "snapshot_interval_s": SNAPSHOT_INTERVAL_S,
            "pending_snapshots": pending,
            "accepted_snapshots": accepted,
            "decks": {
                deck: {
                    "stable_id": acc.stable_id,
                    "audible_s": round(acc.audible_s, 3),
                    "played_fraction": played_fraction(
                        acc.audible_s, acc.duration_ms
                    ),
                    "recorded": acc.event_id is not None,
                    "event_id": acc.event_id,
                    "playing": acc.last_playing,
                    "audible": acc.last_audible,
                    "position_ms": acc.last_position_ms,
                    "title": acc.title,
                    "artist": acc.artist,
                    "last_observed_at": (
                        iso_utc(acc.last_observed_at)
                        if acc.last_observed_at is not None
                        else None
                    ),
                }
                for deck, acc in sorted(self._decks.items())
            },
        }


__all__ = [
    "ADVISORY_DWELL_S",
    "KNOWN_DECK_IDS",
    "MAX_SNAPSHOT_GAP_S",
    "SNAPSHOT_INTERVAL_S",
    "SOURCE_NAME",
    "DeckObservation",
    "DeckObservationError",
    "DeckSnapshot",
    "OpenDjDeckSource",
    "parse_snapshot",
]
