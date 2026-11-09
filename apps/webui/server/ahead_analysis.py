"""Ahead-of-time analysis drain: every track ready BEFORE anyone uses it (NATIVE-21).

A standing engine thread that fills what a listing or a deck would otherwise
have to compute on demand:

0. Phase 0, file tags. A row whose import could not read its tags (packaged
   builds before Thu 1 Oct 2026 had no reader) carries its filename as the
   title, no artist and no duration, and a folder rescan never revisits it.
   This phase re-reads those files' tags into the app's own rows
   (``tag_refresh``; the files are never written).
1. Phase 1, the browser Preview strip. A present track that rekordbox never
   analyzed has no ANLZ strip, and the listing only READS
   ``local-waveform-cache/<sid>.strip.json`` (``local_waveform``). This phase
   calls the same writer a deck load calls (``ensure_local_peaks``), two at a
   time, so the Preview column never depends on a deck load.
2. Phase 2, only once phase 1 is empty: the own_* lanes. Tracks with no
   current record (backend + producer version) are enqueued into the v1
   queue and drained through ``apps.analysis.queue_cli`` (the same runner and
   admission rule the CLI uses), lowest priority, TRACK-MAJOR: the chunk is
   the lane missing for the earliest track (bumped first), so the first
   tracks get every lane before later tracks start any. ``LANE_ORDER`` only
   orders lanes WITHIN a track (cheap and visible first, key after beatgrid,
   which it depends on). Lane-major order (every track's loudness before any
   beatgrid) left a 1274-track import with no BPM or key after 85 min
   (demon-llama, Wed 7 Oct 2026).

Both phases yield while a deck plays or a track was loaded moments ago, using
the coverage drain's own ``DeckGate``. While playing, the drain does ONE thing
(NATIVE-5886): it fills the missing beatgrid/key of the tracks a set is about to
need, AutoPlay's next-up (``set_next_up``) and every track loaded on a deck, one
track per run at the same background QoS, and stops for ``XRUN_COOLDOWN_S``
whenever the page's xrun counter rises. A rise during the in-flight queue call
abandons that call instead of waiting out ``QUEUE_TIMEOUT_S``, and the hold
starts in that same tick. The rest of the library waits for idle. A lane the HOST cannot produce (every
track in a chunk fails the same way, e.g. ffmpeg without soxr) is marked
unavailable with that reason: one log line per transition, never a per-track
storm, never a silent skip. A shared reason that is not a known host cause is
first re-tried one track at a time, so one track that aborts a run cannot
close the lane for every other track. A track that a deck loads or whose
/anlz is read is bumped to the front (``bump``).

``MUSIC_DJ_AHEAD_ANALYSIS`` is a fail-fast enum, ``on`` or ``off``, default on
for the real daemon; ``create_app`` leaves it unarmed so pytest spawns nothing.

Requirements (mini-PRD):
  ✔︎ never-read tags first (NATIVE-21)
    [if] a present row was never tag-read [then] it is re-read before any strip
  ✔︎ strip first, lanes after (NATIVE-21)
    [if] any present unmapped track lacks a strip [then] no lane batch runs
    [if] a track was bumped [then] it is selected before older work
    [if] an ingest wrote new unmapped tracks without a strip [then] their strips
         run next, in ingest order
  ✔︎ never competes with playback, except for the set's next tracks (NATIVE-5886)
    [if] a deck is playing [then] no library-wide work runs
    [if] a deck is playing and AutoPlay's next-up or a deck track lacks beatgrid/key [then] it runs, one track at a time
    [if] the xrun counter rose since the last tick [then] nothing runs for XRUN_COOLDOWN_S
    [if] the counter rose during the boot grace, or first reads above zero there [then] that hold starts too
    [if] the xrun counter rises during an in-flight priority run [then] that run stops and the hold starts
    [if] a deck is playing and the page reports no xrun counter [then] no next-up work runs (paused_no_xrun_counter)
    [if] the page's xrun detector is not armed, or its mirror is older than MIRROR_MAX_AGE_S [then] that is no xrun counter
    [if] a load-settle hold is active [then] nothing runs, even if a deck is playing
  ✔︎ never competes with the launch (PERF-DRAIN-02)
    [if] the engine booted under 120 s ago and no index was served [then] no tick runs
    [if] 120 s have passed [then] the drain ticks, whether or not any index was served
    [if] the first /tracks/index is served [then] the grace ends at once
  ✔︎ one analysis runner at a time (NATIVE-5054)
    [if] a Re-analyze drain's child is running [then] a lane run waits for it
    [if] a deck starts playing while the lane run waits [then] it launches no child once the
         gate opens, cancels its batch, and the tick reports paused_playing
  ✔︎ lane children run in background QoS on macOS (PERF-DRAIN-01)
    [if] the host is darwin [then] the queue child starts under taskpolicy -b
    [if] taskpolicy is missing on darwin [then] the lane fails with a named error
  ✔︎ tracks finish, not lanes (NATIVE-21, Wed 7 Oct 2026)
    [if] the first tracks lack beatgrid/key [then] they get them before later tracks get loudness
    [if] one track lacks several lanes [then] they run in ``LANE_ORDER`` for it
    [if] a track is bumped [then] all its missing lanes run before older work
    [if] the earliest track is ready for key while a later one still lacks beatgrid [then] the key chunk omits the later one
    [if] a later track still lacks loudness [then] it stays out of an earlier track's key chunk
    [if] beatgrid is unavailable [then] key skips tracks that still lack it
  ✔︎ a host-level failure is named once
    [if] every track of a chunk fails one known host way [then] the lane is unavailable
  ✔︎ one track never closes a lane (NATIVE-21)
    [if] a chunk fails one unknown way [then] each track reruns alone before any verdict
    [if] only one track reproduces it alone [then] that track fails and the lane stays open
  ✔︎ a timed-out lane call never holds the import (NATIVE-33)
    [if] a batch times out [then] the next run is another track's lane, never that batch again
    [if] a lane timed out [then] the lanes that did not finish for every track before it runs again
    [if] a set-aside track times out alone too [then] it fails by name and the lane stays open
    [if] a set-aside track is loaded on a deck [then] its retry is the next run, whichever lane holds it
    [if] a lane closes with tracks set aside [then] each fails with the lane reason, none waits forever
  ✔︎ agent parity
    [if] coverage is asked for [then] GET /ahead-analysis/coverage and the CLI agree
  ✔︎ landing events (NATIVE-35)
    [if] a lane batch ran to completion (exit 0 or 1) [then] analysis.changed publishes for every finished id in the batch
    [if] a batch was abandoned, yielded, or exited other than 0 or 1 [then] nothing is published
  ✔︎ coverage never waits on a tick (NATIVE-21)
    [if] a tick is busy [then] coverage answers at once from the last snapshot
  ✔︎ every phase is bounded
    [if] a phase outlives its budget [then] it is abandoned with a named log line
    [if] a phase was abandoned mid-track [then] that track fails, the next tick moves on
    [if] done ids are needed [then] one query per lane per tick, never one per track
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from apps.analysis import queue_store
from apps.shared.env_on_off import read_on_off_environ
from apps.shared.events import publish as publish_engine_event
from apps.shared.process_priority import background_argv, lowered_priority
from apps.shared.shutdown_bound import SpawnAfterQuit
from apps.shared.state.db import StateStoreBusyError, is_sqlite_busy
from apps.webui.server import backfill_drain, enrich_songs, enrich_sources
from apps.webui.server.ahead_analysis_duds import FILENAME as DUDS_FILENAME
from apps.webui.server.ahead_analysis_duds import DudLedger, is_dud
from apps.webui.server.ahead_analysis_phases import PhaseRunner, PhaseStillRunning, PhaseTimeout
from apps.webui.server.ahead_analysis_records import declined_ids, done_ids, library_value_sources
from apps.webui.server.ahead_analysis_siblings import with_path_siblings

log = logging.getLogger(__name__)

AHEAD_ENV: str = "MUSIC_DJ_AHEAD_ANALYSIS"
AHEAD_VALUES: tuple[str, ...] = ("on", "off")
THREAD_NAME: str = "webui.ahead-analysis"
#: Strip decodes per tick, run concurrently (local_waveform's own decode cap).
STRIP_BATCH: int = 2
#: Never-read rows re-tagged per tick; a tag read is milliseconds, no decode.
TAG_BATCH: int = 25
#: Tracks per queue batch in phase 2; small so a deck load waits one batch.
LANE_CHUNK: int = 4
ACTIVE_INTERVAL_S: float = 1.0
PAUSED_INTERVAL_S: float = 5.0
IDLE_INTERVAL_S: float = 60.0
STOP_JOIN_S: float = 5.0
QUEUE_TIMEOUT_S: float = 900.0
#: Budget per tick phase; a phase past it is abandoned with a named log line
#: (ahead_analysis_phases). Lane runs are bounded per subprocess by
#: QUEUE_TIMEOUT_S instead, and a timeout there fails the chunk per track.
PHASE_BUDGETS_S: dict[str, float] = {
    "playing": 10.0,
    "present": 180.0,
    "tags": 120.0,
    "strip": 300.0,
    "plan_idle": 180.0,
    "plan_playing": 180.0,
    "coverage": 300.0,
}
#: Boot grace: no tick and no coverage snapshot for this long after the engine
#: builds the drain, so the first library index at launch never competes with
#: it. The first ``/tracks/index`` served ends it early; nothing extends it,
#: because a headless engine may never serve the index.
STARTUP_GRACE_S: float = 120.0
STARTUP_GRACE_STATE: str = "paused_startup"
#: How old the served coverage snapshot may get before the loop recomputes it.
COVERAGE_REFRESH_S: float = 60.0
NICENESS: int = 19
#: (lane, backend) in drain order. Loudness is cheap. Beatgrid and key are
#: what a new library shows as BPM and key, so they run before waveform:
#: waveform decode was holding the cold beatgrid model past the point a
#: short import had any grid at all.
LANE_ORDER: tuple[tuple[str, str], ...] = (
    ("loudness", "own_loudness.backfill"),
    ("beatgrid", "own_beatgrid.backfill"),
    ("key", "own_key.backfill"),
    ("waveform", "own_waveform.backfill"),
)
#: Predecessors a track must already have before it may join that lane's chunk.
#: Key segmentation reads the beatgrid. Loudness and waveform order work inside
#: one track; they are not a gate on beatgrid or on each other.
LANE_REQUIRES: dict[str, tuple[str, ...]] = {
    "key": ("beatgrid",),
}
STRIP_LANE: str = "strip"
TAGS_LANE: str = "tags"
#: Lanes the drain fills WHILE a deck plays, for the set's next tracks only (NATIVE-5886).
PLAYING_LANES: tuple[str, ...] = ("beatgrid", "key")
#: AutoPlay next-up ids kept; more is library-wide work by another name.
NEXT_UP_MAX: int = 4
#: Newly ingested ids kept at the front behind deck bumps (ENRICH-06).
INGEST_PRIORITY_MAX: int = 5000
#: Any new xrun stops while-playing work this long: playback always wins.
XRUN_COOLDOWN_S: float = 60.0
#: The detector state a page publishes in ``xrun_sentinel.detector`` while its
#: xrun sentinel is installed and has not failed. Only this state makes the
#: published count a live counter (xrun-sentinel.ts ``XrunDetectorState``).
XRUN_DETECTOR_ARMED: str = "armed"
#: Every detector state a page may publish (xrun-sentinel.ts
#: ``XrunDetectorState``). Any other string is contract drift and raises.
XRUN_DETECTOR_STATES: frozenset[str] = frozenset({"unarmed", XRUN_DETECTOR_ARMED, "failed"})
#: A mirror received longer ago than this is no xrun counter. The page
#: publishes once a second and ten seconds is the mirror lease's own TTL
#: (routes/state.py LEASE_TTL_S), so a page past it has stopped publishing.
MIRROR_MAX_AGE_S: float = 10.0
#: How often an in-flight queue child is checked for a new xrun.
XRUN_POLL_S: float = 0.25
#: How long a stopped queue child or worker gets to exit after each signal.
STOP_TREE_GRACE_S: float = 2.0
#: Listings of a suspended tree before it is called still growing.
STOP_TREE_LIST_ROUNDS: int = 20
#: Item states that prove a cancelled batch closed: no runner holds the item
#: and none will claim it. These are the queue's own names. Every other state,
#: one the queue adds later included, is not proof.
CLOSED_ITEM_STATES: frozenset[str] = frozenset(
    {*queue_store.TERMINAL_ITEM_STATES, queue_store.ITEM_CANCELLED, queue_store.ITEM_DEFERRED}
)
PAUSED_XRUN_STATE: str = "paused_xrun"
#: A deck plays and there is while-playing work, but the page reports no xrun
#: counter. With the guard blind the drain does what it did before NATIVE-5886:
#: nothing while a deck plays.
PAUSED_NO_XRUN_COUNTER_STATE: str = "paused_no_xrun_counter"
PRIORITY_PREFIX: str = "priority:"
#: Outcomes after which the loop waits the paused interval.
PAUSED_STATES: frozenset[str] = frozenset(
    {"paused_playing", PAUSED_XRUN_STATE, PAUSED_NO_XRUN_COUNTER_STATE, STARTUP_GRACE_STATE}
)


def arm_from_environ(environ: Mapping[str, str]) -> bool:
    return read_on_off_environ(
        environ,
        env_key=AHEAD_ENV,
        allowed=AHEAD_VALUES,
        default="on",
        normalize=True,
    )


#-----------------------------------------------------------------------------
# pure selection rules
#-----------------------------------------------------------------------------
def front_first(ids: Sequence[str], bumped: Sequence[str]) -> list[str]:
    """``ids`` with any bumped ones first (most recent bump first), order kept."""
    wanted = set(ids)
    head = [sid for sid in dict.fromkeys(reversed(bumped)) if sid in wanted]
    seen = set(head)
    return head + [sid for sid in ids if sid not in seen]


def strip_targets(
    candidates: Sequence[str],
    has_strip: Callable[[str], bool],
    failed: Iterable[str],
    bumped: Sequence[str],
    limit: int = STRIP_BATCH,
) -> list[str]:
    """Up to ``limit`` tracks that still need a strip, bumped first."""
    skip = set(failed)
    picked: list[str] = []
    for sid in front_first(candidates, bumped):
        if sid in skip or has_strip(sid):
            continue
        picked.append(sid)
        if len(picked) >= limit:
            break
    return picked


def _predecessor_blocks(
    sid: str,
    lane: str,
    missing_sets: Mapping[str, set[str]],
    failed_sets: Mapping[str, set[str]],
) -> bool:
    """True when ``lane`` still waits on a predecessor this track has not finished.

    A predecessor that failed terminally for this track does not block, so a
    failed beatgrid does not strand key. An unavailable predecessor is not a
    terminal failure: a track that still lacks it stays blocked, including
    when that lane cannot run on this host.
    """
    for need in LANE_REQUIRES.get(lane, ()):
        if need not in missing_sets:
            continue
        if sid in missing_sets[need] and sid not in failed_sets.get(need, set()):
            return True
    return False


def earliest_eligible_lane(
    sid: str,
    missing_sets: Mapping[str, set[str]],
    failed_sets: Mapping[str, set[str]],
    unavailable: Mapping[str, str],
) -> str | None:
    """The first ``LANE_ORDER`` lane this track may run now, or None."""
    for lane, _backend in LANE_ORDER:
        if lane in unavailable or sid not in missing_sets.get(lane, ()):
            continue
        if sid in failed_sets.get(lane, ()):
            continue
        if _predecessor_blocks(sid, lane, missing_sets, failed_sets):
            continue
        return lane
    return None


def next_lane_work(
    present: Sequence[str],
    missing_by_lane: Mapping[str, Sequence[str]],
    failed_by_lane: Mapping[str, Iterable[str]],
    unavailable: Mapping[str, str],
    bumped: Sequence[str],
    limit: int = LANE_CHUNK,
) -> tuple[str, list[str]] | None:
    """The lane missing for the EARLIEST track (bumped first), and its chunk.

    Track-major: ties on that track go to ``LANE_ORDER``, so one track runs
    loudness, beatgrid, key, waveform in turn, and a library-wide backlog in
    a cheap lane never starves the lanes a DJ reads (BPM, key). A track joins
    the chunk only when this lane is also its earliest eligible missing lane,
    so a later track that still lacks loudness does not ride along in an
    earlier track's key chunk. Predecessors in ``LANE_REQUIRES`` must already
    be current, or have terminally failed for that track. A track that still
    lacks beatgrid stays out of key, including when that lane is unavailable.
    Unavailable is not a terminal failure. A failed beatgrid does not strand key.
    """
    rank_order = front_first(present, bumped)
    rank = {sid: i for i, sid in enumerate(rank_order)}
    missing_sets = {name: set(ids) for name, ids in missing_by_lane.items()}
    failed_sets = {name: set(ids) for name, ids in failed_by_lane.items()}
    earliest: dict[str, str] = {}
    seen: set[str] = set()
    for sid in (*rank_order, *(sid for ids in missing_by_lane.values() for sid in ids)):
        if sid in seen:
            continue
        seen.add(sid)
        lane = earliest_eligible_lane(sid, missing_sets, failed_sets, unavailable)
        if lane is not None:
            earliest[sid] = lane
    if not earliest:
        return None

    def _choice(sid: str) -> tuple[int, int]:
        lane_pos = next(i for i, (name, _backend) in enumerate(LANE_ORDER) if name == earliest[sid])
        return (rank.get(sid, len(rank)), lane_pos)

    lane = earliest[min(earliest, key=_choice)]
    ordered = front_first(missing_by_lane.get(lane, ()), bumped)
    chunk = [sid for sid in ordered if earliest.get(sid) == lane][:limit]
    return (lane, chunk)


#: Host-level causes worth naming verbatim when ffmpeg buries them in stderr.
KNOWN_HOST_CAUSES: tuple[str, ...] = (
    "Requested resampling engine is unavailable",
    "cannot run the pinned resample",
)


def reason_kind(reason: str) -> str:
    """A failure with its track-specific part removed, so a host-wide cause
    (every track failing the same way) is recognisable across a chunk.

    A known host cause anywhere in the text wins (``Cls: <cause>``); else
    ``Cls: <file>: <what failed>: <stderr...>`` becomes ``Cls: <what failed>``;
    anything shaped otherwise
    (e.g. ``TrackVanished: <path> was gone``) is kept whole, i.e. per track.
    """
    # Queue-process failures are already batch-wide. Argparse diagnostics are
    # colon-delimited, so normalizing them would discard the actionable flag.
    if reason.startswith("queue run exit "):
        return reason
    parts = reason.split(": ")
    causes = [cause for cause in KNOWN_HOST_CAUSES if cause in reason]
    if causes:
        label = parts[0] if len(parts) > 1 and " " not in parts[0] else None
        joined = "; ".join(causes)
        return f"{label}: {joined}" if label else joined
    if len(parts) < 3:
        return reason
    return f"{parts[0]}: {parts[2]}"


def _is_known_host_cause(reason: str) -> bool:
    return any(cause in reason for cause in KNOWN_HOST_CAUSES)


#: Queue admission refusals name one track's own facts (its length), never the
#: host: a chunk of duration-less tracks closed beatgrid, loudness and waveform
#: library-wide on silver (Wed 4 Nov 2026). They fail the track, never the lane.
#: ``already_current_version`` is the runner skipping a track the drain thought
#: was not done: a disagreement about one track, never a host fault.
PER_TRACK_REASON_PREFIXES: tuple[str, ...] = ("duration_unknown", "already_current_version")


def _is_per_track_reason(reason: str) -> bool:
    return reason.startswith(PER_TRACK_REASON_PREFIXES)


#: How ``run_lane_via_queue`` words a queue call that outlived QUEUE_TIMEOUT_S.
TIMEOUT_REASON: str = "queue call timed out"


#: Exit codes from which a batch ran to completion: 0, or 1 (the queue CLI's
#: EXIT_TRACK_FAILURES) when at least one track's backend failed. A batch that
#: exited 1 still finished and stored the tracks that succeeded. Exit 1 can
#: also mean a cascade batch failed (NATIVE-6647) while this batch's done ids
#: are still current. Do not import EXIT_TRACK_FAILURES: this module never
#: imports the CLI into the engine process.
BATCH_COMPLETED_EXIT_CODES: tuple[int, ...] = (0, 1)


def _batch_timed_out(chunk: Sequence[str], errors: Mapping[str, str]) -> bool:
    """A multi-track batch whose every track carries the queue-timeout reason."""
    return len(chunk) > 1 and all(errors.get(sid, "").startswith(TIMEOUT_REASON) for sid in chunk)


def _shared_reason(chunk: Sequence[str], errors: Mapping[str, str]) -> str | None:
    """The one reason kind every track of a multi-track chunk failed with, else None."""
    distinct = {reason_kind(why) for why in errors.values()}
    if len(chunk) > 1 and len(errors) == len(chunk) and len(distinct) == 1:
        return distinct.pop()
    return None


def coverage_counts(
    present: Sequence[str], done: Iterable[str], failed: Mapping[str, str]
) -> dict[str, Any]:
    """done / missing / failed (with reasons) over ``present``."""
    present_set = set(present)
    done_set = set(done) & present_set
    failed_here = {sid: why for sid, why in failed.items() if sid in present_set and sid not in done_set}
    reasons: dict[str, int] = {}
    for why in failed_here.values():
        reasons[why] = reasons.get(why, 0) + 1
    return {
        "total": len(present_set),
        "done": len(done_set),
        "missing": len(present_set) - len(done_set) - len(failed_here),
        "failed": len(failed_here),
        "failed_reasons": reasons,
    }


#-----------------------------------------------------------------------------
# the driver
#-----------------------------------------------------------------------------
@dataclass
class AheadStatus:
    state: str = "idle"
    reason: str | None = None
    ticks: int = 0
    strips_written: int = 0
    tags_refreshed: int = 0
    lane_batches: int = 0
    unavailable: dict[str, str] = field(default_factory=dict)
    last_job: dict[str, Any] | None = None
    updated_at: float | None = None
    phase_timeouts: dict[str, int] = field(default_factory=dict)


@dataclass
class AheadSources:
    """The drain's view of the world, injected so tests need no engine."""

    #: Present (playable here) stable_ids, most recent first.
    present_fn: Callable[[], list[str]]
    #: Subset of ids that rekordbox maps (their strip comes from ANLZ).
    mapped_fn: Callable[[Sequence[str]], set[str]]
    has_strip_fn: Callable[[str], bool]
    write_strip_fn: Callable[[str], None]
    #: lane -> ids with a CURRENT own record.
    done_fn: Callable[[str, str], set[str]]
    #: (lane, backend, ids) -> {sid: error} for ids that still lack a record.
    run_lane_fn: Callable[[str, str, list[str]], dict[str, str]]
    playing_fn: Callable[[], bool]
    #: stable_ids whose row was never tag-read (no duration, a file path).
    blank_tags_fn: Callable[[], set[str]]
    #: Re-read one row's file tags; False when the file still reads nothing.
    refresh_tags_fn: Callable[[str], bool]
    #: (lane, backend) -> {sid: reason} for current records whose lane DECLINED
    #: (status failed: key no_tonal_center, beatgrid grid_fit_*). They are
    #: produced, so never re-run, but they are not a value either.
    declined_fn: Callable[[str, str], dict[str, str]]
    #: field -> {sid: source} of real library values (ENRICH-02); None reports no ``usable``.
    library_values_fn: Callable[[str], dict[str, str]] | None = None
    #: present ids -> their song index (ENRICH-03); None reports no ``songs``.
    song_index_fn: Callable[[Sequence[str]], enrich_songs.SongIndex] | None = None
    #: present stable_id -> file path; with it a row whose file a sibling row already
    #: covers counts as done for that lane (#5578). None keeps per-row selection.
    paths_fn: Callable[[], Mapping[str, str]] | None = None
    #: present stable_id -> audio hash as of this call. With paths_fn, a sibling
    #: counts as done only when the hash matches. None keeps the path-only seam.
    #: This map is not the present-row snapshot: that snapshot is up to a minute
    #: old, and a rewritten file would be skipped until it refreshed.
    hashes_fn: Callable[[], Mapping[str, str]] | None = None
    #: Where undecodable files are recorded (AHEAD-DUD-01); None keeps them in memory.
    duds_path: Path | None = None
    #: stable_ids loaded on any deck right now (NATIVE-5886); None reads no decks.
    deck_tracks_fn: Callable[[], list[str]] | None = None
    #: The page's session xrun counter, None when unknown (NATIVE-5886).
    xruns_fn: Callable[[], int | None] | None = None
    #: Drop the cached present-row snapshot so a fresh ingest is seen this tick.
    invalidate_present_fn: Callable[[], None] | None = None


class AheadDrain:
    def __init__(
        self,
        sources: AheadSources,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        phase_budgets_s: Mapping[str, float] = PHASE_BUDGETS_S,
        startup_grace_s: float = 0.0,
        xrun_anchor: list[int | None] | None = None,
    ) -> None:
        self._src = sources
        # ``clock`` stamps what is reported; ``monotonic`` times every deadline,
        # so a wall-clock correction cannot end the grace or an xrun hold early.
        self._clock = clock
        self._monotonic = monotonic
        self._grace_ends_at = monotonic() + startup_grace_s
        self._grace_over = startup_grace_s <= 0
        self._status = AheadStatus()
        self._strip_failed: dict[str, str] = {}
        self._tags_failed: dict[str, str] = {}
        self._lane_failed: dict[str, dict[str, str]] = {lane: {} for lane, _b in LANE_ORDER}
        #: lane -> tracks whose batch timed out, in order; each is retried alone,
        #: once, after everything else (NATIVE-33). Not a failure: key still waits.
        self._lane_set_aside: dict[str, list[str]] = {lane: [] for lane, _b in LANE_ORDER}
        self._duds = DudLedger(sources.duds_path)
        self._bumped: list[str] = []
        #: Ingest-written ids waiting for a strip, in ingest order (#9422).
        #: Strip phase only; deck bumps still go first.
        self._strip_queue: list[str] = []
        #: Ingest batches' ids, oldest batch first (ENRICH-06).
        self._ingest_priority: list[str] = []
        self._next_up: list[str] = []
        self._xruns_seen: int | None = None
        #: The count sampled at the top of this tick, None when there is none.
        self._xrun_tick_sample: int | None = None
        #: True while while-playing work is held because no counter is reported.
        self._xrun_counter_missing: bool = False
        # The production runner reads this list as its xrun baseline. It
        # holds the tick sample only while a while-playing run is in flight,
        # so a rise between that sample and the spawn is not adopted as the
        # new baseline. It is None for every idle run: the idle drain is
        # never abandoned on an xrun.
        self._xrun_anchor = xrun_anchor
        self._xrun_hold_until: float = 0.0
        self._bump_lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._phases = PhaseRunner(phase_budgets_s)
        #: The last computed coverage and when; served as-is by ``coverage()``.
        self._coverage: dict[str, Any] = {"present": None, "rekordbox_mapped": None, "lanes": {}}
        self._coverage_at: float | None = None

    # --- control ----------------------------------------------------------
    def bump(self, stable_id: str) -> None:
        """Move one track to the front of both phases (deck load, /anlz read)."""
        with self._bump_lock:
            if stable_id in self._bumped:
                self._bumped.remove(stable_id)
            self._bumped.append(stable_id)
            del self._bumped[:-50]
        self._wake.set()

    def prioritize(self, stable_ids: Sequence[str]) -> None:
        """Put a fresh ingest batch ahead of the library, behind deck bumps (ENRICH-06).

        Earlier batches keep their place ahead of later ones, so a batch's
        tracks get every playback lane before the next batch starts any.
        """
        with self._bump_lock:
            fresh = [sid for sid in dict.fromkeys(stable_ids) if sid not in set(self._ingest_priority)]
            self._ingest_priority.extend(fresh)
            del self._ingest_priority[:-INGEST_PRIORITY_MAX]
        if self._src.invalidate_present_fn is not None:
            self._src.invalidate_present_fn()
        self._wake.set()

    def queue_strips(self, stable_ids: Sequence[str]) -> list[str]:
        """Queue freshly ingested tracks for the strip phase, in ingest order (#9422).

        Ids that already have a strip, or that rekordbox maps (its ANLZ strip is
        read, never written), are left out, so a re-ingest queues nothing. Returns
        the ids queued.
        """
        ids = list(dict.fromkeys(stable_ids))
        mapped = set(self._src.mapped_fn(ids))
        pending = [
            sid for sid in ids if sid not in mapped and not self._src.has_strip_fn(sid)
        ]
        with self._bump_lock:
            queued = set(self._strip_queue)
            fresh = [sid for sid in pending if sid not in queued]
            self._strip_queue.extend(fresh)
            del self._strip_queue[:-INGEST_PRIORITY_MAX]
        if pending:
            if self._src.invalidate_present_fn is not None:
                self._src.invalidate_present_fn()
            self._wake.set()
        return pending

    def _ranking(self) -> list[str]:
        """Deck bumps first (latest first), then ingest batches in order, as ``front_first`` reads it."""
        with self._bump_lock:
            return [*reversed(self._ingest_priority), *self._bumped]

    def _strip_ranking(self) -> list[str]:
        with self._bump_lock:
            return [
                *reversed(self._ingest_priority),
                *reversed(self._strip_queue),
                *self._bumped,
            ]

    def set_next_up(self, stable_ids: Sequence[str]) -> list[str]:
        """Replace AutoPlay's next-up tracks, the ones analyzed while a deck plays (NATIVE-5886)."""
        with self._bump_lock:
            self._next_up = list(dict.fromkeys(stable_ids))[:NEXT_UP_MAX]
            kept = list(self._next_up)
        self._wake.set()
        return kept

    def wake(self) -> None:
        self._wake.set()

    def index_served(self) -> None:
        """The first library index went out: end the boot grace now (never extends it)."""
        if not self._grace_over:
            self._grace_over = True
            self._wake.set()

    def in_startup_grace(self) -> bool:
        if not self._grace_over and self._monotonic() >= self._grace_ends_at:
            self._grace_over = True
        return not self._grace_over

    def status(self) -> AheadStatus:
        return self._status

    def halted(self) -> bool:
        """True once :meth:`stop` was called."""
        return self._halt.is_set()

    @property
    def sources(self) -> AheadSources:
        """The callables this drain runs on, as built (read-only)."""
        return self._src

    # --- one tick ---------------------------------------------------------
    def tick(self) -> str:
        with self._tick_lock:
            outcome = self._tick()
            self._status.state = outcome
            self._status.ticks += 1
            self._status.updated_at = self._clock()
            return outcome

    def _tick(self) -> str:
        """Every step runs as a bounded phase: past its budget it is abandoned
        (``timeout:<phase>``), and while an abandoned copy is still running the
        tick says so (``waiting:<phase>``) instead of starting a second one."""
        try:
            return self._tick_phases()
        except PhaseTimeout as exc:
            self._fail_timed_out_items(exc)
            return f"timeout:{exc.phase}"
        except PhaseStillRunning as exc:
            return f"waiting:{exc.phase}"
        finally:
            self._status.phase_timeouts = dict(self._phases.timeouts)

    def _tick_phases(self) -> str:
        if self.in_startup_grace():
            self._observe_xruns(in_grace=True)
            return STARTUP_GRACE_STATE
        # The counter is sampled every tick so a rise while idle still starts
        # the hold. A counter that is present in the wrong shape fails the tick
        # whether or not a deck plays: the idle drain never consults the guard,
        # but swallowing the error there would hide wire-contract drift until a
        # deck happened to play.
        xrun_hold = self._observe_xruns()
        if self._phases.run("playing", self._src.playing_fn):
            gate = self._src.playing_fn
            settling = getattr(gate, "settling", None)
            if callable(settling) and settling():
                return "paused_playing"
            playback = getattr(gate, "playback", None)
            if callable(playback) and not playback():
                return "paused_playing"
            return PAUSED_XRUN_STATE if xrun_hold else self._playing_tick()
        bumped = self._ranking()
        strip_ranking = self._strip_ranking()
        present = self._phases.run("present", self._src.present_fn)
        phase_rankings = {TAGS_LANE: bumped, STRIP_LANE: strip_ranking}
        for name, phase in ((TAGS_LANE, self._tags_phase), (STRIP_LANE, self._strip_phase)):
            try:
                if self._phases.run(name, phase, present, phase_rankings[name]):
                    return f"ran:{name}"
            except PhaseStillRunning:
                continue  # that phase is stuck on its own; later phases still run
        work = self._phases.run("plan_idle", self._plan_lane_work, present, bumped)
        if work is None:
            return "green"
        lane, chunk = work
        try:
            self._run_lane(lane, dict(LANE_ORDER)[lane], chunk)
        except LaneRunYielded:
            return "paused_playing"
        return f"ran:{lane}"

    def _observe_xruns(self, *, in_grace: bool = False) -> bool:
        """True while a recent xrun holds while-playing work off (NATIVE-5886).

        A first count read inside the boot grace counts from zero: those xruns
        happened during this boot, so they start the hold too.
        """
        count = None if self._src.xruns_fn is None else self._src.xruns_fn()
        now = self._monotonic()
        if count is not None:
            seen = 0 if self._xruns_seen is None and in_grace else self._xruns_seen
            if seen is not None and count > seen:
                log.info("ahead analysis: %d new xrun(s), while-playing work holds %.0f s",
                         count - seen, XRUN_COOLDOWN_S)
                self._xrun_hold_until = now + XRUN_COOLDOWN_S
            elif seen is not None and count < seen:
                # The counter is per page session: a reload or a mirror
                # leadership move restarts it at zero, so xruns the new page
                # had before this sample are invisible. Fail closed: hold, and
                # rebaseline on the new session's count.
                log.info("ahead analysis: the xrun counter went back (%d -> %d), a new page session; "
                         "while-playing work holds %.0f s", seen, count, XRUN_COOLDOWN_S)
                self._xrun_hold_until = now + XRUN_COOLDOWN_S
            self._xruns_seen = count
        self._xrun_tick_sample = count
        return now < self._xrun_hold_until

    def _next_up_snapshot(self) -> list[str]:
        with self._bump_lock:
            return list(self._next_up)

    def _priority_ids(self) -> list[str]:
        with self._bump_lock:
            next_up = list(self._next_up)
        decks = [] if self._src.deck_tracks_fn is None else self._src.deck_tracks_fn()
        return list(dict.fromkeys([*next_up, *decks]))

    def _playing_tick(self) -> str:
        """One track of beatgrid/key for the set's next tracks, else nothing."""
        targets = self._priority_ids()
        if not targets:
            return "paused_playing"
        if self._xrun_tick_sample is None:
            # No counter is not a counter at zero. Logged once per change.
            if not self._xrun_counter_missing:
                log.warning("ahead analysis: the page reports no xrun counter, while-playing work is held until it does")
            self._xrun_counter_missing = True
            return PAUSED_NO_XRUN_COUNTER_STATE
        if self._xrun_counter_missing:
            log.info("ahead analysis: the page reports an xrun counter again, while-playing work resumes")
        self._xrun_counter_missing = False
        work = self._phases.run("plan_playing", self._plan_priority_work, targets)
        if work is None:
            return "paused_playing"
        lane, sid = work
        self._run_priority(lane, dict(LANE_ORDER)[lane], sid)
        # The queue call can outlive the check at the top of the tick. A rise
        # during that call has already stopped it; start the hold now.
        if self._observe_xruns():
            return PAUSED_XRUN_STATE
        return f"{PRIORITY_PREFIX}{lane}"

    def _plan_priority_work(self, targets: list[str]) -> tuple[str, str] | None:
        missing: dict[str, list[str]] = {}
        for lane, backend in LANE_ORDER:
            if lane not in PLAYING_LANES:
                continue
            # An unavailable lane is not scheduled. Its missing set is still
            # read so key does not run for a track whose beatgrid is neither
            # current nor a terminal per-track failure.
            done = self._lane_done(lane, backend)
            missing[lane] = [sid for sid in targets if sid not in done]
        work = next_lane_work(targets, missing, self._lane_failed, self._status.unavailable, [], limit=1)
        return None if work is None else (work[0], work[1][0])

    def _run_priority(self, lane: str, backend: str, sid: str) -> None:
        """One track alone: its failure fails that track, never the lane.

        The only run an xrun may abandon: the anchor is set for this call and
        cleared after it.
        """
        if self._halt.is_set():
            return
        if self._xrun_anchor is not None:
            self._xrun_anchor[0] = self._xrun_tick_sample
        try:
            errors = self._src.run_lane_fn(lane, backend, [sid])
        except LaneRunYielded:
            return  # the drain stopped while this run waited for the runner gate
        finally:
            if self._xrun_anchor is not None:
                self._xrun_anchor[0] = None
        self._status.lane_batches += 1
        self._status.last_job = {"lane": lane, "ids": [sid], "at": self._clock(), "errors": errors,
                                 "while_playing": True}
        _chunk, errors = self._set_aside_duds(lane, [sid], errors)
        self._lane_failed[lane].update(errors)
        if sid in self._lane_failed[lane] and sid in self._lane_set_aside[lane]:
            # This was the track's one retry alone (NATIVE-33).
            self._lane_set_aside[lane].remove(sid)

    def _fail_timed_out_items(self, exc: PhaseTimeout) -> None:
        """A phase abandoned mid-item fails THAT item, so the next tick moves past it."""
        reason = f"timed out: {exc}"
        if exc.phase == TAGS_LANE and isinstance(exc.item, str):
            self._tags_failed[exc.item] = reason
        elif exc.phase == STRIP_LANE and isinstance(exc.item, list):
            self._strip_failed.update(dict.fromkeys(exc.item, reason))

    def _tags_phase(self, present: list[str], bumped: list[str]) -> bool:
        targets = self._tag_targets(present, bumped)
        if targets:
            self._refresh_tags(targets)
        return bool(targets)

    def _strip_phase(self, present: list[str], bumped: list[str]) -> bool:
        mapped = self._src.mapped_fn(present)
        unmapped = [sid for sid in present if sid not in mapped]
        targets = strip_targets(unmapped, self._src.has_strip_fn, self._strip_failed, bumped)
        if targets:
            self._phases.mark(STRIP_LANE, targets)
            self._write_strips(targets)
        with self._bump_lock:
            self._strip_queue = [
                sid
                for sid in self._strip_queue
                if not self._src.has_strip_fn(sid) and sid not in self._strip_failed
            ]
        return bool(targets)

    def _lane_done(self, lane: str, backend: str) -> set[str]:
        """Rows with the lane's result, plus same-bytes siblings (#5578)."""
        done = self._src.done_fn(lane, backend)
        if self._src.paths_fn is None or self._src.hashes_fn is None:
            return done
        return with_path_siblings(done, self._src.paths_fn(), self._src.hashes_fn())


    def _plan_lane_work(self, present: list[str], bumped: list[str]) -> tuple[str, list[str]] | None:
        # done_fn is one query per lane: read it ONCE, never once per track
        # (silver, Mon 5 Oct 2026: 2270 x 4 queries a tick took over 11 min).
        missing: dict[str, list[str]] = {}
        paths = self._paths()
        for lane, backend in LANE_ORDER:
            current, changed = self._duds.sync(lane, paths)
            self._lane_failed[lane].update(current)
            for sid in changed:
                self._lane_failed[lane].pop(sid, None)
            # Unavailable lanes are not scheduled. Their missing set is still
            # read so key does not run for a track whose beatgrid is neither
            # current nor a terminal per-track failure.
            done = self._lane_done(lane, backend)
            missing[lane] = [sid for sid in present if sid not in done]
        return self._next_work_around_timeouts(present, missing, bumped)

    def _next_work_around_timeouts(
        self, present: list[str], missing: dict[str, list[str]], bumped: list[str]
    ) -> tuple[str, list[str]] | None:
        """``next_lane_work``, with a lane that timed out sent to the back (NATIVE-33).

        A queue call that outlives QUEUE_TIMEOUT_S says the lane is slow here,
        not which track is at fault. Re-running its batch at once kept one
        beatgrid batch on the queue for up to 75 min while no other track got
        any lane (demon-llama, Thu 8 Oct 2026). So, in order: (0) a set-aside
        track that is loaded on a deck is retried alone, whichever lane holds
        it; (1) the lanes with no set-aside track run for every track, the
        tracks that are not set aside first; (2) the slow lane runs for the
        tracks that never timed out; (3) every other set-aside track is
        retried alone, in ``LANE_ORDER``. Step (1) puts set-aside tracks last
        because waveform now follows beatgrid (NATIVE-29): without that, the
        batch that just timed out would get its waveform run next, and a bad
        file could cost one more timeout before any other track moved. A lane that reads
        the slow one (key reads beatgrid) waits with it for those tracks. A
        lane that closed retries nothing, so its set-aside tracks fail with
        the lane's own reason rather than wait forever.
        """
        unavailable = self._status.unavailable
        for lane, held in self._lane_set_aside.items():
            held[:] = [sid for sid in held if sid in missing[lane]]
            if lane in unavailable and held:
                self._lane_failed[lane].update(
                    dict.fromkeys(held, f"lane unavailable on this host: {unavailable[lane]}")
                )
                held.clear()
        retries = [(lane, sid) for lane, _b in LANE_ORDER for sid in self._lane_set_aside[lane]]
        if not retries:
            return next_lane_work(present, missing, self._lane_failed, unavailable, bumped)

        def retry_alone(sid: str) -> tuple[str, list[str]]:
            lane = next(name for name, held_sid in retries if held_sid == sid)  # its earliest lane
            self._lane_set_aside[lane].remove(sid)
            return lane, [sid]

        with self._bump_lock:
            on_deck = list(self._bumped)
        loaded = front_first([sid for _lane, sid in retries if sid in on_deck], on_deck)
        if loaded:
            return retry_alone(loaded[0])
        slow = {lane for lane, _sid in retries}
        waits_on = {lane: {lane, *LANE_REQUIRES.get(lane, ())} & slow for lane, _b in LANE_ORDER}
        quick = {lane: ([] if waits_on[lane] else ids) for lane, ids in missing.items()}
        held_for = {
            lane: {sid for need in waits_on[lane] for sid in self._lane_set_aside[need]} for lane in missing
        }
        rest = {lane: [sid for sid in ids if sid not in held_for[lane]] for lane, ids in missing.items()}
        # Also a track whose solo retry already timed out: it is no longer set
        # aside, and its other lanes are as suspect as its batch mates' are.
        aside = {sid for _lane, sid in retries} | {
            sid
            for failed in self._lane_failed.values()
            for sid, why in failed.items()
            if why.startswith(TIMEOUT_REASON)
        }
        others_first = {lane: [sid for sid in ids if sid not in aside] for lane, ids in quick.items()}
        for candidates in (others_first, quick, rest):
            work = next_lane_work(present, candidates, self._lane_failed, unavailable, bumped)
            if work is not None:
                return work
        return retry_alone(retries[0][1])

    def _tag_targets(self, present: list[str], bumped: list[str]) -> list[str]:
        blank = self._src.blank_tags_fn()
        never_read = [sid for sid in present if sid in blank]
        return [sid for sid in front_first(never_read, bumped) if sid not in self._tags_failed][:TAG_BATCH]

    def _refresh_tags(self, targets: list[str]) -> None:
        errors: dict[str, str] = {}
        for sid in targets:
            self._phases.mark(TAGS_LANE, sid)
            try:
                read = self._src.refresh_tags_fn(sid)
            except sqlite3.OperationalError as exc:
                if not is_sqlite_busy(exc):
                    raise
                # A contended write (seen at boot beside the folder rescan) is
                # about the moment, not the track: the next tick retries it.
                log.info("ahead analysis: tags for %s deferred, state db busy: %s", sid, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - recorded per track, surfaced in coverage
                errors[sid] = f"{type(exc).__name__}: {exc}"
                continue
            if read:
                self._status.tags_refreshed += 1
            else:
                errors[sid] = "the file still reads no tags or no duration"
        self._tags_failed.update(errors)
        self._status.last_job = {"lane": TAGS_LANE, "ids": targets, "at": self._clock(), "errors": errors}

    def _write_strips(self, targets: list[str]) -> None:
        def one(sid: str) -> tuple[str, str | None]:
            try:
                self._src.write_strip_fn(sid)
            except sqlite3.OperationalError as exc:
                if is_sqlite_busy(exc):
                    raise
                return sid, f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"
            except Exception as exc:  # noqa: BLE001 - recorded per track, surfaced in coverage
                return sid, f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"
            if not self._src.has_strip_fn(sid):
                # Without this a writer that "succeeds" but leaves no strip
                # (a track under 0.8 s) would be re-run every second forever.
                return sid, "the decode finished but produced no strip"
            return sid, None

        with ThreadPoolExecutor(max_workers=STRIP_BATCH, thread_name_prefix="ahead-strip") as pool:
            results = list(pool.map(one, targets))
        for sid, error in results:
            if error is None:
                self._status.strips_written += 1
            else:
                self._strip_failed[sid] = error
        self._status.last_job = {"lane": STRIP_LANE, "ids": targets, "at": self._clock(),
                                 "errors": {sid: e for sid, e in results if e}}

    def _run_lane(self, lane: str, backend: str, chunk: list[str]) -> None:
        if self._halt.is_set():
            return
        errors = self._src.run_lane_fn(lane, backend, chunk)
        self._status.lane_batches += 1
        self._status.last_job = {"lane": lane, "ids": chunk, "at": self._clock(), "errors": errors}
        chunk, errors = self._set_aside_duds(lane, chunk, errors)
        per_track = {sid: why for sid, why in errors.items() if _is_per_track_reason(why)}
        if per_track:
            self._lane_failed[lane].update(per_track)
            chunk = [sid for sid in chunk if sid not in per_track]
            errors = {sid: why for sid, why in errors.items() if sid not in per_track}
        if _batch_timed_out(chunk, errors):
            log.warning(
                "ahead analysis: lane %s set aside %s after a timeout; other work runs first, then each alone",
                lane, chunk,
            )
            self._lane_set_aside[lane].extend(sid for sid in chunk if sid not in self._lane_set_aside[lane])
            return
        reason = _shared_reason(chunk, errors)
        if reason is not None and not _is_known_host_cause(reason):
            # One reason for the whole chunk is ambiguous: a host fault, or ONE
            # track's failure that aborted the run and was copied to its
            # siblings (demon-llama, Fri 2 to Mon 5 Oct 2026: beatgrid and key
            # closed for 3 days on one record's contract breach). Each track
            # alone decides it; only a reason every track reproduces alone
            # names the host.
            chunk, errors = self._set_aside_duds(lane, chunk, self._run_singly(lane, backend, chunk))
            reason = _shared_reason(chunk, errors)
        if reason is not None:
            if self._status.unavailable.get(lane) != reason:
                log.warning("ahead analysis: lane %s unavailable on this host: %s", lane, reason)
            self._status.unavailable[lane] = reason
            return
        self._lane_failed[lane].update(errors)

    def _paths(self) -> Mapping[str, str]:
        return {} if self._src.paths_fn is None else self._src.paths_fn()

    def _set_aside_duds(
        self, lane: str, chunk: list[str], errors: dict[str, str]
    ) -> tuple[list[str], dict[str, str]]:
        """Record undecodable files once (AHEAD-DUD-01); the rest decide the lane."""
        duds = {sid: why for sid, why in errors.items() if is_dud(why) and not _is_known_host_cause(why)}
        if duds:
            self._duds.record(lane, duds, self._paths())
            self._lane_failed[lane].update(duds)
        return [sid for sid in chunk if sid not in duds], {k: v for k, v in errors.items() if k not in duds}

    def _run_singly(self, lane: str, backend: str, chunk: list[str]) -> dict[str, str]:
        errors: dict[str, str] = {}
        for sid in chunk:
            if self._halt.is_set() or self._src.playing_fn():
                break
            errors.update(self._src.run_lane_fn(lane, backend, [sid]))
            self._status.lane_batches += 1
        self._status.last_job = {"lane": lane, "ids": chunk, "at": self._clock(), "errors": errors}
        return errors

    def retry_failed(self) -> None:
        self._strip_failed.clear()
        self._tags_failed.clear()
        for failures in self._lane_failed.values():
            failures.clear()
        for held in self._lane_set_aside.values():
            held.clear()
        self._duds.clear()
        if self._status.unavailable:
            log.warning("ahead analysis: re-arming unavailable lanes %s", sorted(self._status.unavailable))
        self._status.unavailable.clear()
        self._wake.set()

    def retry_failed_for(self, stable_ids: Iterable[str]) -> None:
        """Forget these tracks' per-track failures so the next ticks run them again."""
        ids = set(stable_ids)
        for failures in (self._strip_failed, self._tags_failed, *self._lane_failed.values()):
            for sid in ids & set(failures):
                failures.pop(sid)
        self._wake.set()

    def failures(self) -> dict[str, dict[str, str]]:
        """lane -> {stable_id: reason} for every per-track failure the drain holds now."""
        return {
            TAGS_LANE: dict(self._tags_failed),
            STRIP_LANE: dict(self._strip_failed),
            **{lane: dict(failed) for lane, failed in self._lane_failed.items()},
        }

    # --- coverage ---------------------------------------------------------
    def coverage(self) -> dict[str, Any]:
        """The last computed coverage plus the live status. Never queries and
        never waits on a tick, so the HTTP route answers while a tick is busy.
        ``computed_at`` is None until the loop has computed it once."""
        now = self._clock()
        return {
            **self._coverage,
            "state": self._status.state,
            "drain": enrich_sources.lane_drain_states(
                self._status.state, self._coverage.get("lanes", {}), [lane for lane, _b in LANE_ORDER]
            ),
            "strips_written": self._status.strips_written,
            "tags_refreshed": self._status.tags_refreshed,
            "lane_batches": self._status.lane_batches,
            "last_job": self._status.last_job,
            "phase_timeouts": dict(self._status.phase_timeouts),
            "while_playing": {
                "next_up": self._next_up_snapshot(),
                "xrun_hold_s": max(0.0, round(self._xrun_hold_until - self._monotonic(), 1)),
            },
            "computed_at": self._coverage_at,
            "coverage_age_s": None if self._coverage_at is None else round(now - self._coverage_at, 1),
        }

    def refresh_coverage(self) -> dict[str, Any]:
        """Recompute the coverage snapshot (bounded like any phase) and serve it."""
        computed = self._phases.run("coverage", self._compute_coverage)
        self._coverage = computed
        self._coverage_at = self._clock()
        return self.coverage()

    def _refresh_coverage_if_stale(self) -> None:
        if self._coverage_at is not None and self._clock() - self._coverage_at < COVERAGE_REFRESH_S:
            return
        try:
            self.refresh_coverage()
        except (PhaseTimeout, PhaseStillRunning):
            pass  # named in the log by the phase runner; the old snapshot stays served
        except Exception:
            log.exception("ahead analysis: coverage refresh failed; the old snapshot stays served")

    def _compute_coverage(self) -> dict[str, Any]:
        present = self._src.present_fn()
        mapped = self._src.mapped_fn(present)
        unmapped = [sid for sid in present if sid not in mapped]
        blank = self._src.blank_tags_fn()
        #: lane -> (denominator, done, declined, failed): the per-file inputs songs fold.
        inputs: dict[str, enrich_songs.LaneInput] = {
            TAGS_LANE: (present, {sid for sid in present if sid not in blank}, {}, dict(self._tags_failed)),
            STRIP_LANE: (unmapped, {sid for sid in unmapped if self._src.has_strip_fn(sid)}, {},
                         dict(self._strip_failed)),
        }
        lanes: dict[str, Any] = {
            lane: coverage_counts(ids, done, failed) for lane, (ids, done, _d, failed) in inputs.items()
        }
        values: dict[str, dict[str, str]] = {}
        present_set = set(present)
        for lane, backend in LANE_ORDER:
            done = self._lane_done(lane, backend)
            counts = coverage_counts(present, done, dict(self._lane_failed[lane]))
            declined = {
                sid: why for sid, why in self._src.declined_fn(lane, backend).items() if sid in present_set
            }
            reasons: dict[str, int] = {}
            for why in declined.values():
                reasons[why] = reasons.get(why, 0) + 1
            counts["done"] -= len(declined)
            counts["declined"] = len(declined)
            counts["declined_reasons"] = reasons
            counts["unavailable"] = self._status.unavailable.get(lane)
            field = enrich_sources.VALUE_FIELDS.get(lane)
            if field is not None and self._src.library_values_fn is not None:
                own = set(done) - set(declined)
                values[lane] = self._src.library_values_fn(field)
                counts["usable"] = enrich_sources.usable_counts(present, values[lane], own)
            lanes[lane] = counts
            inputs[lane] = (present, set(done), declined, dict(self._lane_failed[lane]))
        out: dict[str, Any] = {"present": len(present), "rekordbox_mapped": len(mapped), "lanes": lanes}
        if self._src.song_index_fn is not None:
            out.update(enrich_songs.drain_song_view(self._src.song_index_fn(present), present, inputs, values, lanes))
        return out

    # --- background loop --------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, name=THREAD_NAME, daemon=True)
        self._thread.start()

    def halt(self) -> None:
        """Start no more analysis runs; returns at once (W43, INSTALL-36).

        The quit's SIGTERM ends the running queue CLI at once. Unhalted, the
        drain read that exit as a chunk failure and forked a fresh CLI per
        track, which nothing then signals, so it outlived the engine.
        """
        self._halt.set()
        self._wake.set()

    def stop(self) -> None:
        self.halt()
        if self._thread is not None:
            self._thread.join(timeout=STOP_JOIN_S)
        self._thread = None
        self._phases.shutdown()

    def _loop(self) -> None:
        interval = ACTIVE_INTERVAL_S
        while not self._halt.is_set():
            self._wake.wait(interval)
            self._wake.clear()
            if self._halt.is_set():
                return
            try:
                outcome = self.tick()
            except (sqlite3.OperationalError, StateStoreBusyError) as exc:
                if isinstance(exc, sqlite3.OperationalError) and not is_sqlite_busy(exc):
                    log.exception("ahead analysis tick failed")
                    self._status.state = "blocked"
                    interval = IDLE_INTERVAL_S
                    continue
                log.info("ahead analysis tick deferred, state db busy: %s", exc)
                interval = ACTIVE_INTERVAL_S
                continue
            except Exception:
                log.exception("ahead analysis tick failed")
                self._status.state = "blocked"
                interval = IDLE_INTERVAL_S
                continue
            finally:
                if not self.in_startup_grace():  # the snapshot is the heavy part; it waits too
                    self._refresh_coverage_if_stale()
            if outcome.startswith(("ran:", PRIORITY_PREFIX)):
                interval = ACTIVE_INTERVAL_S
            elif outcome in PAUSED_STATES or outcome.startswith(("timeout:", "waiting:")):
                interval = PAUSED_INTERVAL_S
            else:
                interval = IDLE_INTERVAL_S


#-----------------------------------------------------------------------------
# engine wiring: real sources
#-----------------------------------------------------------------------------
class MirrorContractError(RuntimeError):
    """The page's ui-mirror carries a field this drain reads, in the wrong shape."""


def mirror_deck_tracks(mirror: Mapping[str, Any] | None) -> list[str]:
    """stable_ids loaded on any deck of the page's ui-mirror (NATIVE-5886).

    No mirror, no ``decks`` and a deck with no track all report nothing. A
    ``decks`` or deck that is present in another shape raises.
    """
    decks = (mirror or {}).get("decks")
    if decks is None:
        return []
    if not isinstance(decks, dict):
        raise MirrorContractError(f"ui-mirror decks is {type(decks).__name__}, expected an object")
    loaded: list[str] = []
    for deck_id, deck in decks.items():
        if not isinstance(deck, dict):
            raise MirrorContractError(f"ui-mirror deck {deck_id!r} is {type(deck).__name__}, expected an object")
        stable_id = deck.get("stable_id")
        if stable_id is None:
            continue
        if not isinstance(stable_id, str):
            raise MirrorContractError(
                f"ui-mirror deck {deck_id!r} stable_id is {type(stable_id).__name__}, expected a string"
            )
        loaded.append(stable_id)
    return loaded


def mirror_age_s(received_monotonic: float | None, *, now: float) -> float:
    """Seconds since the mirror was received. Infinite when none ever was."""
    return float("inf") if received_monotonic is None else now - received_monotonic


def mirror_xruns(mirror: Mapping[str, Any] | None, *, age_s: float) -> int | None:
    """The page's session xrun counter, when it can still rise (NATIVE-5886).

    None when there is no counter to guard with: no mirror, no
    ``xrun_sentinel``, a sentinel with no ``xruns``, a detector that is not
    armed (``failed``, ``unarmed``, or a page too old to say), or a mirror
    received more than MIRROR_MAX_AGE_S ago. A dead detector and a hung page
    both keep showing their last count, so that count is not read as "no new
    xruns". A count or detector state that is present in the wrong shape,
    including a detector string outside XRUN_DETECTOR_STATES, raises, because
    reading it as absent would hide the contract break.
    """
    sentinel = (mirror or {}).get("xrun_sentinel")
    if sentinel is None:
        return None
    if not isinstance(sentinel, dict):
        raise MirrorContractError(f"ui-mirror xrun_sentinel is {type(sentinel).__name__}, expected an object")
    if "xruns" not in sentinel:
        return None
    # XRUNHOLD-01: a page that counts xruns beyond its clock's resolution
    # (``hold_xruns``) is guarded by that count; an older page by every xrun.
    count = sentinel["hold_xruns"] if "hold_xruns" in sentinel else sentinel["xruns"]
    # type() and not isinstance(): JSON true is a bool, and bool is an int.
    if type(count) is not int or count < 0:
        field = "hold_xruns" if "hold_xruns" in sentinel else "xruns"
        raise MirrorContractError(f"ui-mirror xrun_sentinel.{field} is {count!r}, expected a non-negative integer")
    if "detector" not in sentinel:
        return None
    detector = sentinel["detector"]
    if not isinstance(detector, str):
        raise MirrorContractError(f"ui-mirror xrun_sentinel.detector is {detector!r}, expected a string")
    if detector not in XRUN_DETECTOR_STATES:
        raise MirrorContractError(
            f"ui-mirror xrun_sentinel.detector is {detector!r}, expected one of {sorted(XRUN_DETECTOR_STATES)}"
        )
    if detector != XRUN_DETECTOR_ARMED:
        return None
    if age_s > MIRROR_MAX_AGE_S:
        return None
    return count


class LaneAbandoned(Exception):
    """The page's xrun counter rose while a queue child was still running."""


def _read_pipe(stream: Any, parts: list[str]) -> None:
    parts.append(stream.read())


class ProcessTreeNotStopped(RuntimeError):
    """The queue child, or a worker under it, could not be proven stopped."""


def _stop_process_tree(proc: subprocess.Popen[str]) -> None:
    """Stop ``proc`` and the analyzer workers under it, or raise.

    The analyzer runs in a pool under the queue child, and that child can
    start a replacement the moment a worker dies. So nothing is killed until
    the whole tree is suspended: the child first, then every descendant, and
    the listing is repeated until one taken with all of them suspended names
    no new process. A suspended process cannot spawn, so that set is the
    whole tree. Then every member is killed and waited for.

    A tree that cannot be listed, a refused signal, a tree that keeps growing
    and a process that outlives SIGKILL all raise ``ProcessTreeNotStopped``.
    The stop still runs to the end first, so one refusal does not spare the
    rest of the tree.
    """
    if proc.poll() is not None:
        # Gone before any signal, so its workers can no longer be listed. A
        # clean exit closed its own pool. A death by signal did not.
        if proc.returncode < 0:
            raise ProcessTreeNotStopped(
                f"queue child {proc.pid} died on signal {-proc.returncode} before its workers could be listed"
            )
        return
    problems: list[str] = []
    workers = _suspend_tree(proc.pid, problems)
    _signal_workers(workers, "kill", problems)
    proc.kill()
    try:
        proc.wait(timeout=STOP_TREE_GRACE_S)
    except subprocess.TimeoutExpired:
        problems.append(f"queue child {proc.pid} is still running after SIGKILL")
    _gone, alive = psutil.wait_procs(workers, timeout=STOP_TREE_GRACE_S)
    problems.extend(f"worker {worker.pid} is still running after SIGKILL" for worker in alive)
    if problems:
        raise ProcessTreeNotStopped("; ".join(problems))


def _suspend_tree(root_pid: int, problems: list[str]) -> list[psutil.Process]:
    """Suspend ``root_pid`` and every descendant. Return the descendants.

    Only ``root_pid`` and processes listed under it are ever signaled.
    """
    workers: list[psutil.Process] = []
    try:
        root = psutil.Process(root_pid)
        root.suspend()
        for _round in range(STOP_TREE_LIST_ROUNDS):
            fresh = [child for child in _descendants(root) if child not in workers]
            if not fresh:
                return workers
            _signal_workers(fresh, "suspend", problems)
            workers.extend(fresh)
    except psutil.Error as exc:
        problems.append(f"could not suspend and list the tree of queue child {root_pid}: {exc}")
        return workers
    problems.append(
        f"the tree of queue child {root_pid} was still growing after {STOP_TREE_LIST_ROUNDS} listings"
    )
    return workers


def _descendants(root: psutil.Process) -> list[psutil.Process]:
    """Every process under ``root``, read one process at a time when it has to be.

    psutil lists children from one sweep of every process on the host, and on
    Linux a single unreadable ``/proc/<pid>/stat`` (a ``hidepid`` mount, a
    hardened host) fails that sweep with AccessDenied for the whole tree. Our
    own descendants share our uid and stay readable, so the fallback walks the
    host per process and skips only the ones it cannot read. A child older
    than ``root`` is a reused pid, not a descendant, as in psutil's own walk.
    """
    try:
        return root.children(recursive=True)
    except psutil.AccessDenied:
        pass
    root_ctime = root.create_time()
    by_parent: dict[int, list[psutil.Process]] = {}
    for proc in psutil.process_iter(["ppid", "create_time"]):
        ppid, ctime = proc.info["ppid"], proc.info["create_time"]
        if ppid is not None and ctime is not None and ctime >= root_ctime:
            by_parent.setdefault(ppid, []).append(proc)
    found: list[psutil.Process] = []
    seen = {root.pid}
    stack = [root.pid]
    while stack:
        for child in by_parent.get(stack.pop(), []):
            if child.pid not in seen:
                seen.add(child.pid)
                found.append(child)
                stack.append(child.pid)
    return found


def _signal_workers(workers: Sequence[psutil.Process], verb: str, problems: list[str]) -> None:
    """``suspend`` or ``kill`` each worker. One already gone is fine, a refusal is recorded."""
    for worker in workers:
        try:
            getattr(worker, verb)()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as exc:
            problems.append(f"could not {verb} worker {worker.pid}: {exc}")


def _queue_cli(
    args: list[str], db: str, abandon: Callable[[], bool] | None = None
) -> tuple[int, str, str]:
    """Run one queue CLI command, or stop it when ``abandon`` becomes true.

    A timeout still fails that command's tracks. An abandon does not: the
    caller leaves those tracks eligible once the xrun hold ends.

    Idle and while-playing share this loop so a timeout always stops the
    queue child and its spawn-pool workers.
    """
    should_abandon = abandon if abandon is not None else (lambda: False)
    argv = background_argv([sys.executable, "-m", "apps.analysis.queue_cli", "--db", db, "--json", *args])
    env = {**os.environ, "AF_SERVICE_ID": "com.opendj.engine.ahead-analysis"}
    if should_abandon():
        raise LaneAbandoned()
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **lowered_priority(NICENESS),
        env=env,
    )
    out_parts: list[str] = []
    err_parts: list[str] = []
    assert proc.stdout is not None and proc.stderr is not None
    out_thread = threading.Thread(
        target=_read_pipe, args=(proc.stdout, out_parts), name="ahead-queue-stdout", daemon=True
    )
    err_thread = threading.Thread(
        target=_read_pipe, args=(proc.stderr, err_parts), name="ahead-queue-stderr", daemon=True
    )
    out_thread.start()
    err_thread.start()
    deadline = time.monotonic() + QUEUE_TIMEOUT_S
    abandoned = False
    timed_out = False
    try:
        while proc.poll() is None:
            if should_abandon():
                abandoned = True
                _stop_process_tree(proc)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _stop_process_tree(proc)
                break
            try:
                proc.wait(timeout=min(XRUN_POLL_S, remaining))
            except subprocess.TimeoutExpired:
                continue
    finally:
        if proc.poll() is None:
            _stop_process_tree(proc)
        out_thread.join(timeout=5)
        err_thread.join(timeout=5)
    if abandoned:
        raise LaneAbandoned()
    if timed_out or proc.returncode is None:
        raise subprocess.TimeoutExpired(argv, QUEUE_TIMEOUT_S)
    return proc.returncode, "".join(out_parts), "".join(err_parts)


def _last_line(text: str) -> str:
    """The reason a failed run gave: its ``[ERROR]`` message (unwrapped), else
    its last exception line, else its last line."""
    if "[ERROR]" in text:
        return " ".join(text.rsplit("[ERROR]", 1)[1].split())[:300]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    named = [line for line in lines if re.match(r"^[A-Z][\w.]*: ", line)]
    pick = named[-1] if named else (lines[-1] if lines else "no output")
    return pick[:300]


class LaneRunYielded(Exception):
    """A lane run that waited for the runner gate must not start: a deck is
    playing, or the drain was stopped. It launched nothing."""


def run_queued_batch(
    db: str,
    batch_id: str,
    backend: str,
    *,
    yield_fn: Callable[[], bool] | None = None,
    abandon: Callable[[], bool] | None = None,
) -> tuple[int, str, str]:
    """``queue_cli run`` for one batch, once no other analysis runner is running.

    One runner at a time in this daemon (``backfill_drain.RUNNER_GATE``): a
    Re-analyze drain's child sizes its process pool as if it ran alone, and so
    does this one, so two at once would double the memory admission budgets.

    The wait can last as long as a Re-analyze batch, and the tick checked
    playback before it, so ``yield_fn`` (a deck playing, the drain stopped)
    is asked again once the gate is held: if it says yield, no child is
    launched (:class:`LaneRunYielded`) and the gate is released at once.
    ``abandon`` is the xrun guard of :func:`run_lane_via_queue`, passed to the child.
    """
    with backfill_drain.RUNNER_GATE:
        if yield_fn is not None and yield_fn():
            raise LaneRunYielded(f"batch {batch_id} yielded after waiting to run: a deck is playing or the drain stopped")
        return _queue_cli(["run", "--batch-id", batch_id, "--backend", backend], db, abandon=abandon)


def _failure_text(out: str, err: str) -> str:
    """The stream that names why a queue call failed.

    The CLI logs to stderr and prints its own refusal (``[ERROR]``) to
    stdout, so a refusal on stdout outranks a stderr that holds only log lines.
    """
    if "[ERROR]" in out and "[ERROR]" not in err:
        return out
    return err or out


def _cancel_open_batch(db: str, batch_id: str, ids: Sequence[str]) -> None:
    """Close a batch an xrun abandoned. Raise ``RuntimeError`` unless it is proven closed.

    ``ids`` are the tracks the batch was opened with. Proof is a progress
    reply that lists exactly those tracks, each in a ``CLOSED_ITEM_STATES``
    state. A track that is missing, an extra or repeated one, and a state
    outside that set all raise: a short reply or a state this code does not
    know is never read as closed.

    Every failure here is a cleanup failure, so it fails the tick and never a
    track: a timeout must not leave as ``TimeoutExpired``, which the lane
    runner reads as a wedged analysis run.
    """
    # One past the count, so an item beyond the expected ones is not cut off.
    commands = (["cancel", "--batch-id", batch_id], ["progress", "--batch-id", batch_id, "--limit", str(len(ids) + 1)])
    for command in commands:
        verb = command[0]
        try:
            code, out, err = _queue_cli(command, db)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"{verb} of abandoned batch {batch_id} failed: {exc}") from exc
        if code != 0:
            raise RuntimeError(
                f"{verb} of abandoned batch {batch_id} exited {code}: {_last_line(_failure_text(out, err))}"
            )
    items = _progress_items(out, batch_id)
    listed = sorted(item["stable_id"] for item in items)
    if not ids or listed != sorted(ids):
        raise RuntimeError(
            f"progress of batch {batch_id} lists {listed} after cancel, expected exactly {sorted(ids)}"
        )
    not_closed = {item["stable_id"]: item["state"] for item in items if item["state"] not in CLOSED_ITEM_STATES}
    if not_closed:
        raise RuntimeError(
            f"batch {batch_id} not proven closed after cancel: {not_closed} (closed states: {sorted(CLOSED_ITEM_STATES)})"
        )


def _progress_items(out: str, batch_id: str) -> list[dict[str, Any]]:
    """The items of a ``progress`` reply, each with its track and state, or ``RuntimeError``."""
    try:
        body = json.loads(out)
    except ValueError as exc:
        raise RuntimeError(f"progress of batch {batch_id} is not JSON: {_last_line(out)}") from exc
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise RuntimeError(f"progress of batch {batch_id} carries no items list: {_last_line(out)}")
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("state"), str):
            raise RuntimeError(f"progress of batch {batch_id} has an item with no state: {item!r}")
        if not isinstance(item.get("stable_id"), str):
            raise RuntimeError(f"progress of batch {batch_id} has an item with no stable_id: {item!r}")
    return items


def run_lane_via_queue(
    db: str,
    conn_factory: Callable[[], sqlite3.Connection],
    *,
    xruns_fn: Callable[[], int | None] | None = None,
    baseline_fn: Callable[[], int | None] | None = None,
    yield_fn: Callable[[], bool] | None = None,
) -> Callable[[str, str, list[str]], dict[str, str]]:
    """The production lane runner, shared by the idle drain and while-playing runs.

    ``baseline_fn`` arms the xrun guard for one call. The drain makes it
    return the count sampled at the top of the tick, and only for a
    while-playing run. With that baseline, a change in ``xruns_fn`` from it
    (a rise, or a drop that marks a new page session) stops the call before a
    child is spawned, or stops the child in flight.
    The tracks are not failed: the hold retries them, and the batch is
    cancelled so a later tick does not enqueue beside an orphaned one.

    With no baseline (every idle run) nothing reads the counter and the
    call is never abandoned.

    The run itself waits for the daemon's one runner gate (NATIVE-5054,
    :func:`run_queued_batch`). If ``yield_fn`` says yield once the gate is
    held (a deck started, the drain stopped), the batch is cancelled and
    :class:`LaneRunYielded` reaches the tick, which reports ``paused_playing``.
    """

    def abandon_for(baseline: int) -> Callable[[], bool]:
        def abandon() -> bool:
            assert xruns_fn is not None
            count = xruns_fn()
            # A counter that went missing mid-run is a blind guard, so the
            # run stops the same way a rise stops it. So does one that went
            # back: a new page session restarted it, and its xruns before
            # this read cannot be told apart from none.
            return count is None or count != baseline

        return abandon

    def run(lane: str, backend: str, ids: list[str]) -> dict[str, str]:
        baseline = baseline_fn() if baseline_fn is not None and xruns_fn is not None else None
        abandon = None if baseline is None else abandon_for(baseline)
        try:
            return run_bounded(lane, backend, ids, abandon)
        except LaneAbandoned:
            log.warning("ahead analysis: lane %s abandoned, xrun counter rose or went missing during %s", lane, ids)
            return {}
        except SpawnAfterQuit:
            return {}  # the engine is quitting: the tracks are not at fault
        except subprocess.TimeoutExpired as exc:
            # A wedged run fails ITS tracks (and the drain reruns them one by
            # one), never the tick: the child has already been killed.
            log.warning("ahead analysis: lane %s queue call timed out after %g s for %s", lane, exc.timeout, ids)
            return dict.fromkeys(ids, f"queue call timed out after {exc.timeout:g} s")

    def run_bounded(
        lane: str, backend: str, ids: list[str], abandon: Callable[[], bool] | None
    ) -> dict[str, str]:
        id_args = [arg for sid in ids for arg in ("--stable-id", sid)]
        # A rise already visible stops before any child. Enqueue itself is
        # not killed: it is the short write that names the batch, and killing
        # it after the commit can drop that id. A rise during the write is
        # cancelled once the id is in hand, before the analyzer starts.
        if abandon is not None and abandon():
            raise LaneAbandoned()
        code, out, err = _queue_cli(
            ["enqueue", "--lane", lane, "--backend", backend, "--note", "ahead-analysis", *id_args],
            db,
        )
        if code != 0:
            return dict.fromkeys(ids, f"enqueue failed: {_last_line(_failure_text(out, err))}")
        batch_id = json.loads(out)["batch_id"]
        if not isinstance(batch_id, str) or not batch_id:
            raise RuntimeError(f"enqueue returned no batch id: {batch_id!r}")
        try:
            if abandon is not None and abandon():
                raise LaneAbandoned()
            code, out, err = run_queued_batch(db, batch_id, backend, yield_fn=yield_fn, abandon=abandon)
        except (LaneAbandoned, LaneRunYielded, ProcessTreeNotStopped, MirrorContractError):
            # In every case the batch must not stay open for a later tick to
            # enqueue beside. An unstopped tree or an unreadable counter then
            # fails the tick loudly instead of reading as an abandoned lane.
            _cancel_open_batch(db, batch_id, ids)
            raise
        done = done_ids(conn_factory, backend)
        # The analyzer has returned. A counter rise during this cheap read
        # must not discard the reasons the run already recorded.
        if code in BATCH_COMPLETED_EXIT_CODES:
            for stable_id in ids:
                if stable_id in done:
                    publish_engine_event(
                        "analysis.changed",
                        {"stable_id": stable_id, "lane": lane},
                    )
        reasons = _item_reasons(db, batch_id)
        fallback = f"queue run exit {code}: {_last_line(_failure_text(out, err))}"
        return {sid: reasons.get(sid) or fallback for sid in ids if sid not in done}

    return run


def _item_reasons(db: str, batch_id: str) -> dict[str, str]:
    """Each item's own failure reason, so one track's error is never copied to another."""
    code, out, _err = _queue_cli(["progress", "--batch-id", batch_id], db)
    if code != 0:
        return {}
    items = json.loads(out).get("items", [])
    return {
        str(item["stable_id"]): _compact_reason(str(item["reason"]))
        for item in items
        if item.get("reason") and item.get("state") != "done"
    }


def _compact_reason(raw: str) -> str:
    """Cap a reason, keeping any known host cause that sits past the cap."""
    causes = [cause for cause in KNOWN_HOST_CAUSES if cause in raw and cause not in raw[:240]]
    return raw[:240] + "".join(f" ... {cause}" for cause in causes)


def _audio_hashes(
    open_ro: Callable[[], sqlite3.Connection], ids: Sequence[str]
) -> dict[str, str]:
    """stable_id -> audio hash for ids that have one. One query per chunk."""
    if not ids:
        return {}
    conn = open_ro()
    try:
        found: dict[str, str] = {}
        for start in range(0, len(ids), 500):
            chunk = list(ids[start:start + 500])
            marks = ",".join("?" * len(chunk))
            for sid, digest in conn.execute(
                "SELECT stable_id, audio_hash FROM tracks "
                "WHERE deleted_at IS NULL AND audio_hash IS NOT NULL AND "
                f"stable_id IN ({marks})",
                tuple(chunk),
            ):
                if digest:
                    found[str(sid)] = str(digest)
        return found
    finally:
        conn.close()



class PlayableRows:
    """Present ids, paths and rekordbox mapping, refreshed at most once a minute.

    Audio hashes are read from the database on every call. A folder rewrite
    can change one duplicate's hash inside that minute, and a sibling
    decision from a cached hash would skip the new bytes. The read is a few
    milliseconds for a few thousand ids, cheap beside a 1 s tick.
    """

    def __init__(
        self,
        *,
        snapshot_fn: Callable[[], Any],
        open_ro: Callable[[], sqlite3.Connection],
        mapped_fn: Callable[[Sequence[str]], Iterable[str]],
        clock: Callable[[], float] = time.monotonic,
        refresh_s: float = IDLE_INTERVAL_S,
    ) -> None:
        self._snapshot_fn = snapshot_fn
        self._open_ro = open_ro
        self._mapped_fn = mapped_fn
        self._clock = clock
        self._refresh_s = refresh_s
        self._at: float | None = None
        self._ids: list[str] = []
        self._paths: dict[str, str] = {}
        self._mapped: set[str] = set()

    def _ensure(self) -> None:
        now = self._clock()
        if self._at is not None and now - self._at <= self._refresh_s:
            return
        snapshot = self._snapshot_fn()
        self._ids = list(dict.fromkeys(sid for sid, _path in snapshot.on_disk))
        self._paths = dict(snapshot.on_disk)
        self._mapped = set(self._mapped_fn(self._ids))
        self._at = now

    def invalidate(self) -> None:
        """Re-read the snapshot on the next call (a batch was just ingested)."""
        self._at = None

    def present(self) -> list[str]:
        self._ensure()
        return list(self._ids)

    def paths(self) -> dict[str, str]:
        self._ensure()
        return dict(self._paths)

    def mapped(self, ids: Sequence[str]) -> set[str]:
        self._ensure()
        return self._mapped & set(ids)

    def audio_hashes(self) -> dict[str, str]:
        """Current hashes for the present ids. Never the snapshot cache."""
        self._ensure()
        return _audio_hashes(self._open_ro, self._ids)


def build_for_app(app: Any) -> AheadDrain:
    from apps.analysis_waveform import local_waveform
    from apps.shared.state import db as state_db
    from apps.shared.state.ingest import tag_refresh
    from apps.shared.state.writer import StateWriter
    from apps.webui.server.coverage_drain_analysis import DeckGate
    from apps.webui.server.rb_vendor_pkg.track_rows import bulk_rb_meta
    from apps.webui.server.routes import ingest as ingest_routes

    db = str(app.state.state_db_path)
    rows = PlayableRows(
        snapshot_fn=lambda: ingest_routes.build_snapshot(app),
        open_ro=ingest_routes.open_ro,
        mapped_fn=lambda ids: set(bulk_rb_meta(ids)),
    )

    def has_strip(sid: str) -> bool:
        return local_waveform.local_preview_strip(sid)[0] is not None

    def write_strip(sid: str) -> None:
        local_waveform.ensure_local_peaks(sid)

    def mirror() -> Any:
        return getattr(app.state, "ui_mirror", None)

    anchor: list[int | None] = [None]

    def xruns() -> int | None:
        received = getattr(app.state, "ui_mirror_received_monotonic", None)
        return mirror_xruns(mirror(), age_s=mirror_age_s(received, now=time.monotonic()))

    deck_gate = DeckGate(mirror)
    built: list[AheadDrain] = []

    def must_yield() -> bool:
        """Asked by a lane run that has just won the runner gate.

        A while-playing run (its xrun anchor is set) is meant to run with a
        deck playing, so only a stopped drain makes it yield; an idle run
        also yields to a deck that started while it waited.
        """
        if any(drain.halted() for drain in built):
            return True
        return anchor[0] is None and deck_gate()

    def blank_tags() -> set[str]:
        conn = ingest_routes.open_ro()
        try:
            return {row.stable_id for row in tag_refresh.blank_rows(conn)}
        finally:
            conn.close()

    def refresh_tags(sid: str) -> bool:
        conn = state_db.open_rw(Path(db))
        try:
            rows = tag_refresh.blank_rows(conn, [sid])
            if not rows:
                return True
            writer = StateWriter(conn, actor="ahead-analysis-tags")
            try:
                return tag_refresh.refresh_row(writer, rows[0])
            finally:
                writer.close()
        finally:
            conn.close()

    def song_index(ids: Sequence[str]) -> enrich_songs.SongIndex:
        paths = rows.paths()
        return enrich_songs.read_song_index(
            ingest_routes.open_ro, [(sid, paths[sid]) for sid in ids if sid in paths]
        )

    drain = AheadDrain(
        AheadSources(
            present_fn=rows.present,
            mapped_fn=rows.mapped,
            has_strip_fn=has_strip,
            write_strip_fn=write_strip,
            done_fn=lambda _lane, backend: done_ids(ingest_routes.open_ro, backend),
            run_lane_fn=run_lane_via_queue(
                db, ingest_routes.open_ro, xruns_fn=xruns, baseline_fn=lambda: anchor[0], yield_fn=must_yield
            ),
            playing_fn=deck_gate,
            blank_tags_fn=blank_tags,
            refresh_tags_fn=refresh_tags,
            declined_fn=lambda lane, backend: declined_ids(ingest_routes.open_ro, lane, backend),
            library_values_fn=lambda field: library_value_sources(ingest_routes.open_ro, field),
            song_index_fn=song_index,
            paths_fn=rows.paths,
            hashes_fn=rows.audio_hashes,
            duds_path=Path(db).parent / DUDS_FILENAME,
            deck_tracks_fn=lambda: mirror_deck_tracks(mirror()),
            xruns_fn=xruns,
            invalidate_present_fn=rows.invalidate,
        ),
        startup_grace_s=STARTUP_GRACE_S,
        xrun_anchor=anchor,
    )
    built.append(drain)
    return drain


__all__ = [
    "AHEAD_ENV",
    "LANE_ORDER",
    "MIRROR_MAX_AGE_S",
    "NEXT_UP_MAX",
    "PAUSED_NO_XRUN_COUNTER_STATE",
    "PAUSED_XRUN_STATE",
    "PLAYING_LANES",
    "STARTUP_GRACE_S",
    "STARTUP_GRACE_STATE",
    "XRUN_COOLDOWN_S",
    "XRUN_DETECTOR_ARMED",
    "XRUN_DETECTOR_STATES",
    "AheadDrain",
    "AheadSources",
    "LaneRunYielded",
    "PlayableRows",
    "arm_from_environ",
    "build_for_app",
    "coverage_counts",
    "front_first",
    "mirror_age_s",
    "mirror_deck_tracks",
    "mirror_xruns",
    "next_lane_work",
    "run_queued_batch",
    "strip_targets",
]
