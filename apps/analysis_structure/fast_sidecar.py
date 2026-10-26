"""Own-fast sections at serve time: compute once per track and grid, cache, never block a load.

OFF BY DEFAULT. Nothing here runs unless ``MDT_OWN_FAST_SECTIONS=1``
(``ENV_FLAG``). With the flag off ``/anlz`` serves exactly what it did before.

WHERE THE INPUTS COME FROM. The downbeats are the ones the deck is about to
draw: the ``beatgrid`` block of the ``/anlz`` payload being served (rekordbox
PQTZ or the own grid, whichever the beatgrid lane chose), so a section line
always lands on a bar line the user can see. The peaks are the app's own
3-band waveform peaks (``apps.analysis_waveform.local_waveform``), from its
cache when this track was decoded before, else from one decode.

NEVER ON THE LOAD PATH. A cached sidecar for the same grid is served at once.
Otherwise the work goes to one background thread. When the track's waveform
peaks are already cached the request waits at most ``INLINE_WAIT_S`` for it
(the segmenter itself takes tens of milliseconds); when a decode is ahead it
does not wait at all and answers ``status: pending``, and the worker announces
the result on the event bus (``library.changed`` kind ``sections``) so a
loaded deck refetches. A decoder still busy after ``BUSY_RETRIES`` retries
or a decode that cannot run at all, ends in an announced ``failed`` answer
held in memory for ``FAILURE_HOLD_S`` (never written: it is about the moment).
One worker, so a library scroll cannot
start a decode storm, and a track already queued is not queued twice.

THE CACHE KEY. The sidecar records the producer version, a digest of the
downbeats it was cut on, and a digest of the audio file it was cut from
(``local_waveform.audio_revision``). A different grid (the user re-gridded, or
switched the beatgrid lane), a new producer version or replaced audio
recomputes; nothing else does. Audio that cannot be resolved is a miss, not a
hit: the old cut is never served for a file nobody can compare it with.

-Claude
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeGuard

from ._io import atomic_write_json, utc_now_iso_seconds
from .fast_segment import PRODUCER, PRODUCER_VERSION, segment
from .pipeline import cache_dir

log = logging.getLogger(__name__)

ENV_FLAG = "MDT_OWN_FAST_SECTIONS"
SCHEMA = "structure-fast/v2"  # v2: audio_rev
INLINE_WAIT_S = 0.25
BUSY_RETRIES = 3  # a busy decode is retried this many times, BUSY_RETRY_S apart and growing
BUSY_RETRY_S = 5.0
MAX_QUEUED = 4  # jobs waiting behind the running one; older waiting ones are cancelled
FAILURE_HOLD_S = 300.0  # a decode failure is served from memory this long, then a load retries
MAX_RETRY_TIMERS = MAX_QUEUED  # sleeping busy-decoder retries; past this the oldest is dropped and announced

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="own-fast-sections")
_INFLIGHT: dict[tuple[str, str], Future[dict[str, Any]]] = {}
_INFLIGHT_LOCK = threading.Lock()
_HELD_FAILURES: dict[tuple[str, str], tuple[float, dict[str, Any], str | None]] = {}  # (held at, doc, audio_rev)
_RETRY_TIMERS: dict[tuple[str, str], threading.Timer] = {}  # insertion order = age
# The audio revision each sleeping retry's failed attempt read before its
# decode, so a retry displaced at the cap is held against THAT audio.
_RETRY_REVISIONS: dict[tuple[str, str], str | None] = {}


def enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip() == "1"


def fast_dir(data_dir: Path) -> Path:
    return cache_dir(data_dir) / "own-fast"


def fast_path(data_dir: Path, stable_id: str) -> Path:
    return fast_dir(data_dir) / f"{stable_id}.json"


def downbeats_of(payload: dict[str, Any]) -> list[float]:
    """Downbeat times (``n == 1``) of the beatgrid block the payload will draw."""
    grid = payload.get("beatgrid") or {}
    return [float(b["t"]) for b in grid.get("beats") or () if int(b.get("n", 0)) == 1]


def grid_digest(downbeats: list[float]) -> str:
    text = ",".join(f"{t:.3f}" for t in downbeats)
    return hashlib.sha256(text.encode("ascii")).hexdigest()[:16]


class CacheUnreadable(Exception):
    """The sidecar file exists but cannot be read: a storage fault, not a miss."""


def _quarantine(path: Path, why: str) -> None:
    """Move a corrupt sidecar aside, kept for inspection, so the recompute cannot destroy it."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    kept = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        os.replace(path, kept)
    except FileNotFoundError:  # a concurrent request already moved it aside
        return
    log.error("own-fast cache %s is corrupt (%s); quarantined as %s", path, why, kept.name)


def _load(data_dir: Path, stable_id: str) -> dict[str, Any] | None:
    """The cached doc, or None when there is none for this producer schema.

    - absent, or an older ``structure-fast/*`` schema: a miss, recomputed.
    - a read error (permissions, I/O): raises ``CacheUnreadable``; the caller
      reports it and nothing overwrites the file.
    - corrupt (not JSON, not an object, not a structure-fast doc, or a
      current-schema doc ``_doc_problem`` rejects: another track's id, no
      status, ok without sections): moved
      aside by ``_quarantine`` and logged as an error, then a miss, so the
      evidence survives and the track still heals on recompute.
    """
    path = fast_path(data_dir, stable_id)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CacheUnreadable(f"{path}: {exc}") from exc
    try:
        doc = json.loads(text)
    except ValueError as exc:
        _quarantine(path, f"not JSON: {exc}")
        return None
    schema = doc.get("schema") if isinstance(doc, dict) else None
    if not isinstance(schema, str) or not schema.startswith("structure-fast/"):
        _quarantine(path, f"not a structure-fast doc (schema {schema!r})")
        return None
    if schema != SCHEMA:
        log.info("own-fast cache %s is %s, not %s: recomputing", path, schema, SCHEMA)
        return None
    problem = _doc_problem(doc, stable_id)
    if problem is not None:
        _quarantine(path, problem)
        return None
    return doc


SECTION_KEYS = ("start_s", "end_s", "label", "start_bar")
MOMENT_KEYS = ("kind", "time_s", "bar")


def _has(item: object, keys: tuple[str, ...]) -> bool:
    return isinstance(item, dict) and all(k in item for k in keys)


def _seconds(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _index(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _optional(s: dict[str, Any], key: str, ok: Any) -> bool:
    return key not in s or s[key] is None or ok(s[key])


def _section_ok(s: dict[str, Any]) -> bool:
    """Every field serving reads (``serve._own_phrase``, ``drop_events.mark_drops``) has its served type."""
    energy = s.get("energy")
    return (
        _seconds(s["start_s"]) and _seconds(s["end_s"]) and s["end_s"] > s["start_s"]
        and isinstance(s["label"], str) and _index(s["start_bar"])
        and _optional(s, "dj_label", lambda v: isinstance(v, str))
        and _optional(s, "bars", _index)
        and _optional(s, "ambient", lambda v: isinstance(v, bool))
        and _optional(s, "energy", lambda v: isinstance(v, dict))
        and (not isinstance(energy, dict) or _optional(energy, "score_db", _seconds))
    )  # fmt: skip


def _moment_ok(m: dict[str, Any]) -> bool:
    return isinstance(m["kind"], str) and _seconds(m["time_s"]) and _index(m["bar"])


def _ok_doc_problem(doc: dict[str, Any]) -> str | None:
    sections, moments = doc.get("sections"), doc.get("moments")
    if not isinstance(sections, list) or not isinstance(moments, list):
        return "status ok without sections and moments lists"
    if not sections:
        return "status ok with no sections"  # the segmenter always cuts at least one
    if not all(_has(s, SECTION_KEYS) for s in sections):
        return "a section lacks start_s, end_s, label or start_bar"
    if not all(_has(m, MOMENT_KEYS) for m in moments):
        return "a moment lacks kind, time_s or bar"
    if not all(_section_ok(s) for s in sections):
        return "a section has a non-finite or reversed bound, a non-text label or a non-integer bar"
    if not all(_moment_ok(m) for m in moments):
        return "a moment has a non-text kind, a non-finite time or a non-integer bar"
    return None


def _doc_problem(doc: dict[str, Any], stable_id: str) -> str | None:
    """Why a current-schema doc cannot be served for ``stable_id``, or None when it can."""
    if doc.get("stable_id") != stable_id:
        return f"stable_id {doc.get('stable_id')!r} is not {stable_id!r}"
    for key in ("grid_digest", "producer", "producer_version", "audio_rev"):
        if not isinstance(doc.get(key), str):
            return f"{key} missing"
    if doc["producer"] != PRODUCER:
        return f"producer {doc['producer']!r} is not {PRODUCER!r}"
    status = doc.get("status")
    if status == "ok":
        return _ok_doc_problem(doc)
    if status == "failed":
        return None if isinstance(doc.get("reason"), str) else "status failed without a reason"
    return f"status {status!r} is neither ok nor failed"


def _write(data_dir: Path, doc: dict[str, Any]) -> None:
    path = fast_path(data_dir, doc["stable_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, doc, encoding="utf-8", newline="\n")


def _base(stable_id: str, digest: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "stable_id": stable_id,
        "producer": PRODUCER,
        "producer_version": PRODUCER_VERSION,
        "grid_digest": digest,
        "analyzed_at": utc_now_iso_seconds(),
    }


def compute(stable_id: str, downbeats: list[float], data_dir: Path, attempt: int = 0) -> dict[str, Any]:
    """Decode or read the peaks, segment, write the sidecar. Runs on the worker."""
    from apps.analysis_waveform.decode import DETAIL_COLUMNS_PER_S, LocalDecodeUnavailable
    from apps.analysis_waveform.local_waveform import audio_revision, ensure_local_peaks

    base = _base(stable_id, grid_digest(downbeats))
    # Read before the peaks: if the file changes mid-job the doc names the
    # older revision and the next load recuts, never the reverse.
    revision = audio_revision(stable_id)
    try:
        peaks = ensure_local_peaks(stable_id)
    except LocalDecodeUnavailable as exc:
        if exc.retryable:
            # A statement about this moment, not the track: not cached. A deck
            # that already got `pending` only refetches on the announce, so
            # try again later rather than leave it pending until a reload.
            if attempt < BUSY_RETRIES:
                _schedule_retry(stable_id, downbeats, data_dir, attempt + 1, base["grid_digest"], revision)
                return {**base, "status": "pending", "reason": f"decode_busy: {exc.reason}"}
            _cancel_retry((stable_id, base["grid_digest"]))  # terminal: a sleeping retry must not cut again
            return _hold_failure(stable_id, base, f"decode_busy_retries_exhausted: {exc.reason}", revision=revision)
        _cancel_retry((stable_id, base["grid_digest"]))
        return _hold_failure(stable_id, base, f"no_peaks: {exc.reason}", revision=revision)
    except Exception as exc:  # any other peak-load crash: held against the revision read above
        log.error("own-fast sections job for %s failed", stable_id, exc_info=exc)
        return _crashed(stable_id, base, exc, revision)
    try:
        result = segment(peaks, downbeats, peaks.shape[0] / DETAIL_COLUMNS_PER_S)
        doc = {**base, **result}
        if revision is not None:
            doc["audio_rev"] = revision
            _write(data_dir, doc)
    except Exception as exc:  # a crash after the revision read: held against THAT audio, not the file now
        log.error("own-fast sections job for %s failed", stable_id, exc_info=exc)
        return _crashed(stable_id, base, exc, revision)
    _cancel_retry((stable_id, base["grid_digest"]))
    _announce(stable_id)
    return doc


def _crashed(stable_id: str, base: dict[str, Any], exc: Exception, revision: str | None) -> dict[str, Any]:
    """An unexpected failure inside ``compute`` (logged by the caller), held against its pre-decode revision."""
    _cancel_retry((stable_id, base["grid_digest"]))
    return _hold_failure(stable_id, base, f"error: {type(exc).__name__}: {exc}", revision=revision)


def _schedule_retry(
    stable_id: str, downbeats: list[float], data_dir: Path, attempt: int, digest: str, revision: str | None
) -> None:
    """One sleeping retry per (track, grid), at most ``MAX_RETRY_TIMERS`` in all.

    A scroll through undecoded rows while the decoder is saturated would
    otherwise hold one sleeping thread per row per retry level. The oldest
    retry past the cap is cancelled and becomes a held, announced failure, so a
    deck still holding ``pending`` gets an answer instead of a fresh cycle.
    That failure is held against ``revision``, the audio the busy attempt
    read, so a file replaced while the retry slept is recut, not refused.
    """
    key = (stable_id, digest)

    def fire() -> None:
        with _INFLIGHT_LOCK:
            if _RETRY_TIMERS.get(key) is not timer:
                return  # displaced while waiting for the lock: already held as failed
            del _RETRY_TIMERS[key]
            _RETRY_REVISIONS.pop(key, None)
        _submit(stable_id, downbeats, data_dir, attempt)

    dropped: list[tuple[tuple[str, str], str | None]] = []
    with _INFLIGHT_LOCK:
        if key in _RETRY_TIMERS:
            return
        while len(_RETRY_TIMERS) >= MAX_RETRY_TIMERS:
            oldest = next(iter(_RETRY_TIMERS))
            _RETRY_TIMERS.pop(oldest).cancel()
            dropped.append((oldest, _RETRY_REVISIONS.pop(oldest, None)))
        timer = threading.Timer(BUSY_RETRY_S * attempt, fire)
        timer.daemon = True
        _RETRY_TIMERS[key] = timer
        _RETRY_REVISIONS[key] = revision
        timer.start()
    for (dropped_id, dropped_digest), dropped_rev in dropped:
        _hold_failure(
            dropped_id,
            _base(dropped_id, dropped_digest),
            "decode_busy_displaced: retry cap reached",
            revision=dropped_rev,
        )


def _cancel_retry(key: tuple[str, str]) -> None:
    """Drop a sleeping retry for a (track, grid) another attempt just finished.

    Otherwise it would later cut again and announce again, and each announce
    refetches a loaded deck."""
    with _INFLIGHT_LOCK:
        timer = _RETRY_TIMERS.pop(key, None)
        _RETRY_REVISIONS.pop(key, None)
    if timer is not None:
        timer.cancel()


_RESOLVE_NOW: Any = object()  # _hold_failure: no attempt revision known, read it now


def _hold_failure(stable_id: str, base: dict[str, Any], reason: str, *, revision: Any = _RESOLVE_NOW) -> dict[str, Any]:
    """A terminal ``failed`` answer for a decode that could not run, announced and held in memory.

    Never written to the sidecar: a busy decoder, an unmounted drive or a
    missing ffmpeg is a fact about this moment, not about the track, and a
    written failure would match its grid digest forever. Held for
    ``FAILURE_HOLD_S`` so a refetch does not restart the work at once; the
    first load after that tries again.
    """
    from apps.analysis_waveform.local_waveform import audio_revision

    doc = {**base, "status": "failed", "reason": reason}
    # Held against the audio as it resolves now: a relink or replacement
    # inside the hold must recut, not keep answering this failure.
    # The revision the failed attempt read BEFORE its decode, when known: a
    # file replaced mid-attempt must not inherit the old bytes' failure.
    if revision is _RESOLVE_NOW:
        revision = audio_revision(stable_id)
    with _INFLIGHT_LOCK:
        _HELD_FAILURES[(stable_id, base["grid_digest"])] = (time.monotonic(), doc, revision)
    _announce(stable_id)
    return doc


def _announce(stable_id: str) -> None:
    """Tell clients this track's sections changed, so a loaded deck refetches.

    The request that started the work may already have answered ``pending``;
    without this the deck would keep that answer until it next loads the track.
    """
    from apps.shared.events import publish

    publish("library.changed", {"kind": "sections", "ids": [stable_id]})


def _submit(stable_id: str, downbeats: list[float], data_dir: Path, attempt: int = 0) -> Future[dict[str, Any]]:
    """Queue one job per (track, grid); beyond ``MAX_QUEUED`` waiting, the OLDEST waiting ones are cancelled.

    Every selected row prefetches ``/anlz``, so arrowing down a list of
    undecoded tracks would otherwise queue a full decode per row, and the
    track finally loaded would wait behind all of them. A cancelled job is
    announced (``_on_done``) so a deck still holding its ``pending`` answer
    refetches, which queues it again as the newest.
    """
    key = (stable_id, grid_digest(downbeats))
    with _INFLIGHT_LOCK:
        fut = _INFLIGHT.get(key)
        if fut is not None and not fut.done():  # a finished one is not joined: its answer may be stale
            return fut
        fut = _EXECUTOR.submit(compute, stable_id, downbeats, data_dir, attempt)
        _INFLIGHT[key] = fut
        waiting = [f for f in _INFLIGHT.values() if not f.running() and not f.done()]
        stale = waiting[: max(len(waiting) - MAX_QUEUED, 0)]
    # Outside the lock: a future that is already done (or cancelled below) runs
    # its callback right here, and _on_done takes the same (non-reentrant) lock.
    fut.add_done_callback(lambda _f: _on_done(key, fut))
    for old in stale:
        old.cancel()
    return fut


def _on_done(key: tuple[str, str], fut: Future[dict[str, Any]]) -> None:
    """Drop the finished job; a cancelled or crashed one is announced, never left silent.

    ``compute`` turns every expected outcome into a doc, so an exception here
    is a defect (segmentation, the write, the publish): it is logged with its
    traceback and held as a ``failed`` answer, because the request that
    started the job may already have told a deck ``pending``.
    """
    with _INFLIGHT_LOCK:
        if _INFLIGHT.get(key) is fut:
            del _INFLIGHT[key]
    stable_id, digest = key
    if fut.cancelled():
        _announce(stable_id)
        return
    exc = fut.exception()
    if exc is None:
        return
    log.error("own-fast sections job for %s failed", stable_id, exc_info=exc)
    _hold_failure(stable_id, _base(stable_id, digest), f"error: {type(exc).__name__}: {exc}")


def _peaks_cached(stable_id: str) -> bool:
    """Whether this track's waveform peaks are cached AND current for its audio now (no decode)."""
    from apps.analysis_waveform.local_waveform import peaks_entry_current

    return peaks_entry_current(stable_id)


def _cut_from_current_audio(doc: dict[str, Any], stable_id: str) -> bool:
    """Whether ``doc`` was cut from the bytes this id resolves to now (no decode).

    Unresolvable audio is a miss: the recompute then reports why, rather than
    the old cut hiding a missing or repointed file."""
    from apps.analysis_waveform.local_waveform import audio_revision

    now = audio_revision(stable_id)
    return now is not None and doc.get("audio_rev") == now


def _held_failure(key: tuple[str, str], hold_s: float = FAILURE_HOLD_S) -> dict[str, Any] | None:
    """A held failure for this (track, grid), unless it expired or the audio it was about changed."""
    from apps.analysis_waveform.local_waveform import audio_revision

    revision = audio_revision(key[0])
    with _INFLIGHT_LOCK:
        held = _HELD_FAILURES.get(key)
        if held is None:
            return None
        if time.monotonic() - held[0] < hold_s and held[2] == revision:
            return held[1]
        del _HELD_FAILURES[key]
        return None


def _is_current(doc: dict[str, Any] | None, digest: str, stable_id: str) -> TypeGuard[dict[str, Any]]:
    """Whether a cached doc answers for this grid, this producer and today's audio."""
    return (
        doc is not None
        and doc.get("grid_digest") == digest
        and doc.get("producer_version") == PRODUCER_VERSION
        and _cut_from_current_audio(doc, stable_id)
    )


def fast_sections(
    payload: dict[str, Any],
    stable_id: str,
    data_dir: Path,
    *,
    wait_s: float = INLINE_WAIT_S,
    hold_s: float = FAILURE_HOLD_S,
) -> dict[str, Any]:
    """The own-fast sidecar for this track on this payload's grid, or a pending/failed note.

    ``hold_s`` is how long a held decode failure answers before a load retries;
    production keeps ``FAILURE_HOLD_S``, a caller may pass a different real hold."""
    downbeats = downbeats_of(payload)
    if not downbeats:
        return {"status": "failed", "reason": "no_beatgrid", "producer": PRODUCER}
    digest = grid_digest(downbeats)
    try:
        doc = _load(data_dir, stable_id)
    except CacheUnreadable as exc:
        log.error("own-fast cache unreadable for %s: %s", stable_id, exc)
        return {"status": "failed", "reason": f"cache_unreadable: {exc}", "producer": PRODUCER}
    if _is_current(doc, digest, stable_id):
        return doc
    held = _held_failure((stable_id, digest), hold_s)
    if held is not None:
        return held
    # Read BEFORE submitting: a fast worker could cache the peaks in between,
    # and a request that arrived with a decode ahead must not wait at all.
    if not _peaks_cached(stable_id):
        wait_s = 0.0
    pending = {"status": "pending", "reason": "computing", "producer": PRODUCER, "producer_version": PRODUCER_VERSION}
    fut = _submit(stable_id, downbeats, data_dir)
    try:
        doc = fut.result(timeout=wait_s)
    except (FutureTimeout, CancelledError):  # cancelled: superseded by newer selections, announced
        return pending
    except Exception as exc:  # reported on the wire, never a 500 on /anlz
        log.exception("own-fast sections failed for %s", stable_id)
        return {"status": "failed", "reason": f"error: {type(exc).__name__}: {exc}"}
    if doc.get("status") == "ok" and not _cut_from_current_audio(doc, stable_id):
        # The job joined was cutting bytes since replaced: cut again, announce when done.
        _submit(stable_id, downbeats, data_dir)
        return pending
    return doc


__all__ = ["ENV_FLAG", "SCHEMA", "CacheUnreadable", "compute", "downbeats_of", "enabled", "fast_sections"]
