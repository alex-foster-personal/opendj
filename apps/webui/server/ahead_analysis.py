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
   admission rule the CLI uses), lowest priority, in ``LANE_ORDER``: the cheap
   visible lanes before the ~10 s a track ones.

Both phases yield while a deck plays or a track was loaded moments ago, using
the coverage drain's own ``DeckGate``. A lane the HOST cannot produce (every
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
  ✔︎ never competes with playback
    [if] a deck is playing [then] the tick runs nothing
  ✔︎ cheap lanes before expensive ones
    [if] loudness/waveform are missing anywhere [then] beatgrid/key wait
  ✔︎ a host-level failure is named once
    [if] every track of a chunk fails one known host way [then] the lane is unavailable
  ✔︎ one track never closes a lane (NATIVE-21)
    [if] a chunk fails one unknown way [then] each track reruns alone before any verdict
    [if] only one track reproduces it alone [then] that track fails and the lane stays open
  ✔︎ agent parity
    [if] coverage is asked for [then] GET /ahead-analysis/coverage and the CLI agree
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

from apps.shared.process_priority import lowered_priority
from apps.webui.server.ahead_analysis_records import declined_ids, done_ids
from apps.webui.server.ahead_analysis_phases import PhaseRunner, PhaseStillRunning, PhaseTimeout

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
    "plan": 180.0,
    "coverage": 300.0,
}
#: How old the served coverage snapshot may get before the loop recomputes it.
COVERAGE_REFRESH_S: float = 60.0
NICENESS: int = 19
#: (lane, backend) in drain order: cheap and visible first.
LANE_ORDER: tuple[tuple[str, str], ...] = (
    ("loudness", "own_loudness.backfill"),
    ("waveform", "own_waveform.backfill"),
    ("beatgrid", "own_beatgrid.backfill"),
    ("key", "own_key.backfill"),
)
STRIP_LANE: str = "strip"
TAGS_LANE: str = "tags"


def arm_from_environ(environ: Mapping[str, str]) -> bool:
    raw = environ.get(AHEAD_ENV, "on").strip().lower()
    if raw not in AHEAD_VALUES:
        raise ValueError(f"{AHEAD_ENV}={raw!r} is not a member of {AHEAD_VALUES}")
    return raw == "on"


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


def next_lane_work(
    missing_by_lane: Mapping[str, Sequence[str]],
    failed_by_lane: Mapping[str, Iterable[str]],
    unavailable: Mapping[str, str],
    bumped: Sequence[str],
    limit: int = LANE_CHUNK,
) -> tuple[str, list[str]] | None:
    """The first lane in ``LANE_ORDER`` with attemptable work, and its chunk."""
    for lane, _backend in LANE_ORDER:
        if lane in unavailable:
            continue
        skip = set(failed_by_lane.get(lane, ()))
        todo = [sid for sid in front_first(missing_by_lane.get(lane, ()), bumped) if sid not in skip]
        if todo:
            return lane, todo[:limit]
    return None


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


class AheadDrain:
    def __init__(
        self,
        sources: AheadSources,
        *,
        clock: Callable[[], float] = time.time,
        phase_budgets_s: Mapping[str, float] = PHASE_BUDGETS_S,
    ) -> None:
        self._src = sources
        self._clock = clock
        self._status = AheadStatus()
        self._strip_failed: dict[str, str] = {}
        self._tags_failed: dict[str, str] = {}
        self._lane_failed: dict[str, dict[str, str]] = {lane: {} for lane, _b in LANE_ORDER}
        self._bumped: list[str] = []
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

    def wake(self) -> None:
        self._wake.set()

    def status(self) -> AheadStatus:
        return self._status

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
        if self._phases.run("playing", self._src.playing_fn):
            return "paused_playing"
        with self._bump_lock:
            bumped = list(self._bumped)
        present = self._phases.run("present", self._src.present_fn)
        for name, phase in ((TAGS_LANE, self._tags_phase), (STRIP_LANE, self._strip_phase)):
            try:
                if self._phases.run(name, phase, present, bumped):
                    return f"ran:{name}"
            except PhaseStillRunning:
                continue  # that phase is stuck on its own; later phases still run
        work = self._phases.run("plan", self._plan_lane_work, present, bumped)
        if work is None:
            return "green"
        lane, chunk = work
        self._run_lane(lane, dict(LANE_ORDER)[lane], chunk)
        return f"ran:{lane}"

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
        return bool(targets)

    def _plan_lane_work(self, present: list[str], bumped: list[str]) -> tuple[str, list[str]] | None:
        # done_fn is one query per lane: read it ONCE, never once per track
        # (silver, Mon 5 Oct 2026: 2270 x 4 queries a tick took over 11 min).
        missing: dict[str, list[str]] = {}
        for lane, backend in LANE_ORDER:
            if lane in self._status.unavailable:
                continue
            done = self._src.done_fn(lane, backend)
            missing[lane] = [sid for sid in present if sid not in done]
        return next_lane_work(missing, self._lane_failed, self._status.unavailable, bumped)

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
                if "locked" not in str(exc) and "busy" not in str(exc):
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
        errors = self._src.run_lane_fn(lane, backend, chunk)
        self._status.lane_batches += 1
        self._status.last_job = {"lane": lane, "ids": chunk, "at": self._clock(), "errors": errors}
        reason = _shared_reason(chunk, errors)
        if reason is not None and not _is_known_host_cause(reason):
            # One reason for the whole chunk is ambiguous: a host fault, or ONE
            # track's failure that aborted the run and was copied to its
            # siblings (demon-llama, Fri 2 to Mon 5 Oct 2026: beatgrid and key
            # closed for 3 days on one record's contract breach). Each track
            # alone decides it; only a reason every track reproduces alone
            # names the host.
            errors = self._run_singly(lane, backend, chunk)
            reason = _shared_reason(chunk, errors)
        if reason is not None:
            if self._status.unavailable.get(lane) != reason:
                log.warning("ahead analysis: lane %s unavailable on this host: %s", lane, reason)
            self._status.unavailable[lane] = reason
            return
        self._lane_failed[lane].update(errors)

    def _run_singly(self, lane: str, backend: str, chunk: list[str]) -> dict[str, str]:
        errors: dict[str, str] = {}
        for sid in chunk:
            if self._src.playing_fn():
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
        if self._status.unavailable:
            log.warning("ahead analysis: re-arming unavailable lanes %s", sorted(self._status.unavailable))
        self._status.unavailable.clear()
        self._wake.set()

    # --- coverage ---------------------------------------------------------
    def coverage(self) -> dict[str, Any]:
        """The last computed coverage plus the live status. Never queries and
        never waits on a tick, so the HTTP route answers while a tick is busy.
        ``computed_at`` is None until the loop has computed it once."""
        now = self._clock()
        return {
            **self._coverage,
            "state": self._status.state,
            "strips_written": self._status.strips_written,
            "tags_refreshed": self._status.tags_refreshed,
            "lane_batches": self._status.lane_batches,
            "last_job": self._status.last_job,
            "phase_timeouts": dict(self._status.phase_timeouts),
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
        lanes: dict[str, Any] = {
            TAGS_LANE: coverage_counts(
                present, [sid for sid in present if sid not in blank], dict(self._tags_failed)
            ),
            STRIP_LANE: coverage_counts(
                unmapped, [sid for sid in unmapped if self._src.has_strip_fn(sid)], dict(self._strip_failed)
            ),
        }
        present_set = set(present)
        for lane, backend in LANE_ORDER:
            counts = coverage_counts(present, self._src.done_fn(lane, backend), dict(self._lane_failed[lane]))
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
            lanes[lane] = counts
        return {"present": len(present), "rekordbox_mapped": len(mapped), "lanes": lanes}

    # --- background loop --------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, name=THREAD_NAME, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()
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
            except Exception:
                log.exception("ahead analysis tick failed")
                self._status.state = "blocked"
                interval = IDLE_INTERVAL_S
                continue
            finally:
                self._refresh_coverage_if_stale()
            if outcome.startswith("ran:"):
                interval = ACTIVE_INTERVAL_S
            elif outcome == "paused_playing" or outcome.startswith(("timeout:", "waiting:")):
                interval = PAUSED_INTERVAL_S
            else:
                interval = IDLE_INTERVAL_S


#-----------------------------------------------------------------------------
# engine wiring: real sources
#-----------------------------------------------------------------------------
def _queue_cli(args: list[str], db: str) -> tuple[int, str, str]:
    argv = [sys.executable, "-m", "apps.analysis.queue_cli", "--db", db, "--json", *args]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=QUEUE_TIMEOUT_S, check=False,
        **lowered_priority(NICENESS),
        env={**os.environ, "AF_SERVICE_ID": "com.opendj.engine.ahead-analysis"},
    )
    return proc.returncode, proc.stdout, proc.stderr


def _last_line(text: str) -> str:
    """The reason a failed run gave: its ``[ERROR]`` message (unwrapped), else
    its last exception line, else its last line."""
    if "[ERROR]" in text:
        return " ".join(text.rsplit("[ERROR]", 1)[1].split())[:300]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    named = [line for line in lines if re.match(r"^[A-Z][\w.]*: ", line)]
    pick = named[-1] if named else (lines[-1] if lines else "no output")
    return pick[:300]


def run_lane_via_queue(
    db: str, conn_factory: Callable[[], sqlite3.Connection]
) -> Callable[[str, str, list[str]], dict[str, str]]:
    def run(lane: str, backend: str, ids: list[str]) -> dict[str, str]:
        try:
            return run_bounded(lane, backend, ids)
        except subprocess.TimeoutExpired as exc:
            # A wedged run fails ITS tracks (and the drain reruns them one by
            # one), never the tick: subprocess.run has already killed it.
            log.warning("ahead analysis: lane %s queue call timed out after %g s for %s", lane, exc.timeout, ids)
            return dict.fromkeys(ids, f"queue call timed out after {exc.timeout:g} s")

    def run_bounded(lane: str, backend: str, ids: list[str]) -> dict[str, str]:
        id_args = [arg for sid in ids for arg in ("--stable-id", sid)]
        code, out, err = _queue_cli(
            ["enqueue", "--lane", lane, "--backend", backend, "--note", "ahead-analysis", *id_args], db
        )
        if code != 0:
            return dict.fromkeys(ids, f"enqueue failed: {_last_line(err or out)}")
        batch_id = json.loads(out)["batch_id"]
        code, out, err = _queue_cli(["run", "--batch-id", batch_id, "--backend", backend], db)
        done = done_ids(conn_factory, backend)
        reasons = _item_reasons(db, batch_id)
        fallback = f"queue run exit {code}: {_last_line(err or out)}"
        return {sid: reasons.get(sid) or fallback for sid in ids if sid not in done}

    return run


def _compact_reason(raw: str) -> str:
    """Cap a reason, keeping any known host cause that sits past the cap."""
    causes = [cause for cause in KNOWN_HOST_CAUSES if cause in raw and cause not in raw[:240]]
    return raw[:240] + "".join(f" ... {cause}" for cause in causes)


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


def build_for_app(app: Any) -> AheadDrain:
    from apps.analysis_waveform import local_waveform
    from apps.shared.state import db as state_db
    from apps.shared.state.ingest import tag_refresh
    from apps.shared.state.writer import StateWriter
    from apps.webui.server.coverage_drain_analysis import DeckGate
    from apps.webui.server.rb_vendor_pkg.track_rows import bulk_rb_meta
    from apps.webui.server.routes import ingest as ingest_routes

    db = str(app.state.state_db_path)
    cache: dict[str, Any] = {"at": 0.0, "ids": [], "mapped": set()}

    def present() -> list[str]:
        # The coverage snapshot is the engine's one "playable here" measure;
        # refreshed at most once a minute so a busy tick costs no rescan.
        if time.monotonic() - cache["at"] > IDLE_INTERVAL_S:
            snapshot = ingest_routes.build_snapshot(app)
            cache["ids"] = list(dict.fromkeys(sid for sid, _path in snapshot.on_disk))
            cache["mapped"] = set(bulk_rb_meta(cache["ids"]))
            cache["at"] = time.monotonic()
        return list(cache["ids"])

    def has_strip(sid: str) -> bool:
        return local_waveform.local_preview_strip(sid)[0] is not None

    def mapped(ids: Sequence[str]) -> set[str]:
        return cache["mapped"] & set(ids)

    def write_strip(sid: str) -> None:
        local_waveform.ensure_local_peaks(sid)

    def mirror() -> Any:
        return getattr(app.state, "ui_mirror", None)

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

    return AheadDrain(
        AheadSources(
            present_fn=present,
            mapped_fn=mapped,
            has_strip_fn=has_strip,
            write_strip_fn=write_strip,
            done_fn=lambda _lane, backend: done_ids(ingest_routes.open_ro, backend),
            run_lane_fn=run_lane_via_queue(db, ingest_routes.open_ro),
            playing_fn=DeckGate(mirror),
            blank_tags_fn=blank_tags,
            refresh_tags_fn=refresh_tags,
            declined_fn=lambda lane, backend: declined_ids(ingest_routes.open_ro, lane, backend),
        )
    )


__all__ = [
    "AHEAD_ENV",
    "LANE_ORDER",
    "AheadDrain",
    "AheadSources",
    "arm_from_environ",
    "build_for_app",
    "coverage_counts",
    "front_first",
    "next_lane_work",
    "strip_targets",
]
