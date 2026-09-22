"""Analysis-derived read endpoints (gating unit: analysis router).

Implements the data side of two downstream features:

  * auto-cue-proposals   -> GET /api/v1/tracks/{stable_id}/auto-cues
  * anlz-fallback-beatgrid -> GET /api/v1/tracks/{stable_id}/beatgrid-fallback

Both endpoints are read-only projections of the ``analysis`` table in
``state.db`` (written exclusively by :mod:`apps.analysis`). This module
NEVER writes state.db - it opens the file ``mode=ro`` with
``query_only`` on, matching :func:`apps.shared.state.db.open_ro`.

Contract notes for downstream builders
--------------------------------------
* ``/auto-cues`` items are PROPOSALS, never committed cues. The response
  carries a top-level ``proposal: true`` and every item carries
  ``source`` so a UI can render them distinctly from djmdCue hot cues.
* ``/beatgrid-fallback`` returns ``beatgrid`` in EXACTLY the ANLZ shape
  served by ``GET /tracks/{stable_id}/anlz`` (rb_vendor._beatgrid_payload):
  ``{"beat_count": N, "beats": [{"n": 1..4, "bpm": <2dp>, "t": <3dp s>}]}``
  so clients can swap sources without a schema branch. ``anlz_available``
  tells the client whether the authoritative rekordbox grid also exists
  (prefer ``/anlz`` when it does).
* Fail-fast: 404s are explicit ``{"code", "message"}`` details (same
  shape as rb_vendor.not_found). ``/auto-cues`` returns HTTP 200 with
  ``proposals: []`` and ``backend="none"`` when the track is in the
  library but has no analysis row yet (no proposals, not an error).
  ``/beatgrid-fallback`` never invents a grid - no analysis downbeats
  and no ANLZ means 404, not a synthesised guess.

Test injection points (mirrors the ``lock_status_fn`` pattern in deps.py):
  * ``app.state.analysis_db_path``  -> Path of the state DB to read
    (default: apps.shared.paths.STATE_DB).
  * ``app.state.anlz_available_fn`` -> Callable[[str], bool] overriding
    the rekordbox ANLZ on-disk probe.

The integrator wires ``router`` into ``create_app()`` under ``/api/v1``.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from apps.analysis.auto_cues import propose_cues
from apps.analysis.canonical import canonical_pointer
from apps.analysis.record import AnalysisRecord
from apps.shared.paths import STATE_DB

from .. import rb_vendor
from ..backend import NotFoundError, StateBackend
from ..deps import get_read_state

router = APIRouter(prefix="/tracks", tags=["analysis"])

AUTO_CUES_SOURCE: str = "apps.analysis.auto_cues"
UNANALYZED_BACKEND: str = "none"
UNANALYZED_BACKEND_VERSION: str = "none"
BEATGRID_SOURCE: str = "apps.analysis"
BEATS_PER_BAR: int = 4          # 4/4 assumed, matching ANLZ PQTZ n=1..4
_MIN_BEAT_INTERVAL_S: float = 0.05   # < 50 ms/beat (1200 BPM) = corrupt record

_CACHE_ANALYSIS = "no-store"    # analysis rows can be re-run at any time


# ----- response models --------------------------------------------------------

class AutoCueOut(BaseModel):
    """One PROPOSED cue. ``kind`` is intro|drop|break|outro|'' (unlabelled)."""

    time_s: float
    kind: str
    confidence: float
    source: str
    rms_dbfs: float


class AutoCuesOut(BaseModel):
    stable_id: str
    proposal: Literal[True] = True   # never committed cues - render distinctly
    source: str
    backend: str
    backend_version: str
    proposals: List[AutoCueOut]


class FallbackBeatOut(BaseModel):
    """ANLZ beat fields plus ``extrapolated``.

    PQTZ ``/anlz`` beats stay ``{n, bpm, t}``. This fallback endpoint always
    sends ``extrapolated`` so a client never has to guess which beats are tail.
    """

    n: int      # beat-in-bar 1..4
    bpm: float  # rounded 2dp, like /anlz
    t: float    # seconds, rounded 3dp, like /anlz
    extrapolated: bool


class FallbackBeatgridOut(BaseModel):
    """Identical shape to the ``beatgrid`` object served by /anlz.

    ``source`` is fixed at ``"own"``: every grid this endpoint can serve came
    from an ``apps.analysis`` record (an unmapped track has no rekordbox PQTZ
    to read), and the ANLZ contract this shape mirrors makes the field
    REQUIRED (``AnlzBeatgridSource`` in ``anlz-types.ts``). Omitting it here
    left the frontend type assertion in ``beatgrid-fallback-api.ts`` hiding a
    real mismatch: a fail-closed, source-aware reader would reject this grid
    outright (Codex P2 BLOCKING, PR #1587).
    """

    source: Literal["own"]
    status: Literal["ok"]
    beat_count: int
    beats: List[FallbackBeatOut]


class BeatgridFallbackOut(BaseModel):
    stable_id: str
    source: str
    backend: str
    backend_version: str
    bpm: float
    bpm_confidence: float
    anlz_available: bool
    beatgrid: FallbackBeatgridOut


# ----- helpers ----------------------------------------------------------------

def _analysis_db_path(request: Request) -> Path:
    override: Path | None = getattr(request.app.state, "analysis_db_path", None)
    return Path(override) if override is not None else STATE_DB


def _open_analysis_ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise HTTPException(
            status_code=500,
            detail={
                "code": "STATE_DB_UNAVAILABLE",
                "message": f"state DB missing on disk: {path}",
            },
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _load_latest_record(
    db_path: Path, stable_id: str, backend: str | None
) -> AnalysisRecord | None:
    """Newest analysis row for ``stable_id`` (deterministic tie-break).

    Returns None when the track has no analysis row (or the ``analysis``
    table itself does not exist yet - the pipeline has never run). Both
    are the same client-visible state: nothing to serve.
    """
    conn = _open_analysis_ro(db_path)
    try:
        # EXCLUDE own_* rows. This query takes the newest row across EVERY
        # backend, and native-analysis v1 puts per-lane own records in the
        # same `analysis` table, so without this an own KEY or LOUDNESS row
        # written after a beatgrid run would win here and hand
        # /beatgrid-fallback and /auto-cues that row's empty `downbeats_s`
        # (reproduced Wed 9 Sep 2026: a librosa row with 4 downbeats was
        # shadowed by an own_key.inapp row with none). These two endpoints
        # are pre-v1 readers of the LEGACY flat record; own records are read
        # through the canonical pointer instead, and teaching them the own
        # beatgrid lane belongs to that lane's PR, not this one. The explicit
        # `backend=` filter is untouched: a caller that names a backend gets
        # exactly what it named.
        sql = (
            "SELECT record_json FROM analysis WHERE stable_id = ?"
        )
        params: list[object] = [stable_id]
        if backend is not None:
            sql += " AND backend = ?"
            params.append(backend)
        else:
            sql += " AND backend NOT LIKE 'own\\_%' ESCAPE '\\'"
        sql += " ORDER BY analyzed_at DESC, backend ASC, backend_version DESC LIMIT 1"
        try:
            row = conn.execute(sql, params).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: analysis" in str(exc):
                return None
            raise
    finally:
        conn.close()
    if row is None:
        return None
    return AnalysisRecord.from_json(row[0])


def _canonical_own_beatgrid_lane(
    db_path: Path, stable_id: str,
) -> "LaneResult | None":
    """The canonical own ``beatgrid`` lane result, or None when unset.

    Reads through the production read-only state-db path and the stored
    ``analysis_canonical`` pointer. A pointer naming a missing row or a row
    without its named lane is corruption and propagates as ``RuntimeError``.
    """
    from apps.analysis.lanes import LaneResult

    conn = _open_analysis_ro(db_path)
    try:
        pointer = canonical_pointer(conn, stable_id, "beatgrid")
        if pointer is None:
            return None
        row = conn.execute(
            "SELECT record_json FROM analysis "
            "WHERE stable_id = ? AND backend = ? AND backend_version = ?",
            (stable_id, pointer[0], pointer[1]),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"canonical beatgrid pointer for {stable_id} names "
                f"{pointer[0]}@{pointer[1]} but no such analysis row exists"
            )
        result = AnalysisRecord.from_json(row[0]).lanes.get("beatgrid")
        if result is None:
            raise RuntimeError(
                f"canonical beatgrid record {pointer[0]}@{pointer[1]} for "
                f"{stable_id} carries no 'beatgrid' lane"
            )
        return result
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    finally:
        conn.close()


def _own_beatgrid_failure_blocks_legacy(db_path: Path, stable_id: str) -> bool:
    """Whether a failed native own beatgrid determination blocks legacy fallback.

    A successful native own record does NOT block this endpoint's default
    lookup: while own is not yet the serving source, the legacy row keeps the
    deck grid monotonic across backfill (STANDALONE-04). Only a terminal
    ``failed`` own determination refuses the pre-v1 row, so a superseded
    legacy grid cannot mask v1's named failure (discussion_r3975326241).
    """
    lane = _canonical_own_beatgrid_lane(db_path, stable_id)
    return lane is not None and lane.status == "failed"


def _default_anlz_available(stable_id: str) -> bool:
    """Disk truth: does a rekordbox ANLZ .DAT exist for this track?

    ``VENDOR_MAPPING_NOT_FOUND`` (track known but not a rekordbox track)
    and ``MASTER_DB_UNAVAILABLE`` (no rekordbox install on this machine)
    both mean "no ANLZ" by definition, not an error. ``TRACK_NOT_FOUND``
    and everything else propagates - an unknown stable_id stays a 404.
    """
    try:
        content = rb_vendor.resolve_content(stable_id)
    except HTTPException as exc:
        code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
        if code in ("VENDOR_MAPPING_NOT_FOUND", "MASTER_DB_UNAVAILABLE"):
            return False
        raise
    if content.analysis_data_path is None:
        return False
    return rb_vendor.resolve_share_path(content.analysis_data_path).is_file()


def _anlz_available(request: Request, stable_id: str) -> bool:
    fn: Callable[[str], bool] | None = getattr(
        request.app.state, "anlz_available_fn", None
    )
    if fn is not None:
        return bool(fn(stable_id))
    return _default_anlz_available(stable_id)


def _empty_auto_cues(stable_id: str) -> AutoCuesOut:
    return AutoCuesOut(
        stable_id=stable_id,
        source=AUTO_CUES_SOURCE,
        backend=UNANALYZED_BACKEND,
        backend_version=UNANALYZED_BACKEND_VERSION,
        proposals=[],
    )


def _track_in_library(backend: StateBackend, stable_id: str) -> bool:
    try:
        backend.get_track(stable_id)
    except NotFoundError:
        return False
    else:
        return True


def _invalid_record(stable_id: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=500,
        detail={
            "code": "ANALYSIS_RECORD_INVALID",
            "message": f"analysis record for {stable_id} is corrupt: {message}",
        },
    )


def synthesize_fallback_beats(record: AnalysisRecord) -> list[FallbackBeatOut] | None:
    """Derive an ANLZ-shaped beat list from measured analysis data.

    Anchored on ``downbeats_s`` (n=1 on every detected downbeat, beats
    2..4 interpolated inside each bar), extended past the last downbeat
    at the last measured bar tempo (or ``record.bpm`` when only one
    downbeat exists). Beats past the last detected downbeat are marked
    ``extrapolated: true`` on the wire. The grid starts at the FIRST
    detected downbeat - beats before it are not invented.

    Returns None when nothing measurable anchors a grid (no downbeats,
    or a single downbeat with no usable BPM). Corrupt data (negative
    duration, non-increasing downbeats, absurd tempo) raises via the
    caller - it is never smoothed over.

    Empty ``downbeats_s`` still returns None. This function does not
    invent a tempo map and does not call ``fit_tempo_map`` or
    ``apply_tempo_map``.
    """
    downbeats = [float(d) for d in record.downbeats_s]
    if not downbeats:
        return None
    if record.duration_s <= 0:
        raise _invalid_record(record.stable_id, f"duration_s={record.duration_s}")
    for a, b in zip(downbeats, downbeats[1:], strict=False):
        if b <= a:
            raise _invalid_record(
                record.stable_id, f"downbeats_s not strictly increasing ({a} -> {b})"
            )

    beats: list[FallbackBeatOut] = []
    from .analysis_fallback_beats import fallback_emit, fallback_tail_beats

    # Bars between consecutive measured downbeats.
    for start, end in zip(downbeats, downbeats[1:], strict=False):
        bar_s = end - start
        if bar_s / BEATS_PER_BAR < _MIN_BEAT_INTERVAL_S:
            raise _invalid_record(
                record.stable_id, f"bar of {bar_s:.4f}s implies >1200 BPM"
            )
        for k in range(BEATS_PER_BAR):
            fallback_emit(beats, start + k * bar_s / BEATS_PER_BAR, k + 1, bar_s)

    return fallback_tail_beats(record, downbeats, beats)


# ----- endpoints --------------------------------------------------------------

@router.get("/{stable_id}/auto-cues", response_model=AutoCuesOut)
def get_auto_cues(
    stable_id: str,
    request: Request,
    backend: str | None = Query(
        None, description="Analysis backend to read (default: newest row)"
    ),
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> AutoCuesOut:
    """PROPOSED hot cues from apps.analysis (META-04). Never committed cues.

    Returns HTTP 200 with ``proposals: []`` and ``backend="none"`` when the
    track is in the library but has no analysis row yet. Unknown stable_id
    and unmatched ``?backend=`` still 404 with ``ANALYSIS_NOT_FOUND``.
    """
    record = _load_latest_record(_analysis_db_path(request), stable_id, backend)
    if record is None:
        if backend is None and _track_in_library(_backend, stable_id):
            return _empty_auto_cues(stable_id)
        raise rb_vendor.not_found(
            "ANALYSIS_NOT_FOUND",
            f"no apps.analysis record for stable_id {stable_id}"
            + (f" with backend {backend}" if backend else "")
            + "; run the analysis pipeline first",
        )
    tp = propose_cues(record)
    return AutoCuesOut(
        stable_id=stable_id,
        source=AUTO_CUES_SOURCE,
        backend=record.backend,
        backend_version=tp.backend_version,
        proposals=[
            AutoCueOut(
                time_s=c.time_s, kind=c.label, confidence=c.confidence,
                source=AUTO_CUES_SOURCE, rms_dbfs=c.rms_dbfs,
            )
            for c in tp.cues
        ],
    )


@router.get("/{stable_id}/beatgrid-fallback", response_model=BeatgridFallbackOut)
def get_beatgrid_fallback(
    stable_id: str,
    request: Request,
    backend: str | None = Query(
        None, description="Analysis backend to read (default: newest row)"
    ),
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> BeatgridFallbackOut:
    """Analysis-derived beatgrid in the exact /anlz ``beatgrid`` shape.

    Serves the apps.analysis grid whenever one is derivable (with
    ``anlz_available`` reporting whether the authoritative rekordbox grid
    also exists). 404 with an explicit code when no grid can be served -
    a beatgrid is never invented.

    A caller naming ``backend=`` gets exactly what it named. The default
    newest-row lookup instead refuses a superseded pre-v1 legacy row only
    when the canonical own beatgrid lane is ``failed``; a successful native
    backfill does not disable the legacy row while own is not yet serving
    (STANDALONE-04).
    """
    anlz_ok = _anlz_available(request, stable_id)
    db_path = _analysis_db_path(request)
    own_failure_blocks = (
        backend is None and _own_beatgrid_failure_blocks_legacy(db_path, stable_id)
    )
    record = None if own_failure_blocks else _load_latest_record(db_path, stable_id, backend)
    beats = synthesize_fallback_beats(record) if record is not None else None
    if record is None or beats is None:
        if own_failure_blocks:
            reason = "own beatgrid analysis failed for this track"
        elif record is None:
            reason = "no apps.analysis record"
        else:
            reason = "analysis record has no usable downbeats"
        raise HTTPException(
            status_code=404,
            detail={
                "code": "BEATGRID_FALLBACK_NOT_FOUND",
                "message": (
                    f"{reason} for stable_id {stable_id}; "
                    + (
                        "rekordbox ANLZ exists - use GET "
                        f"/api/v1/tracks/{stable_id}/anlz"
                        if anlz_ok
                        else "no rekordbox ANLZ either - nothing to serve "
                             "(a beatgrid is never invented)"
                    )
                ),
                "anlz_available": anlz_ok,
            },
        )
    return BeatgridFallbackOut(
        stable_id=stable_id,
        source=BEATGRID_SOURCE,
        backend=record.backend,
        backend_version=record.backend_version,
        bpm=record.bpm,
        bpm_confidence=record.bpm_confidence,
        anlz_available=anlz_ok,
        beatgrid=FallbackBeatgridOut(
            source="own", status="ok", beat_count=len(beats), beats=beats
        ),
    )


__all__ = [
    "AUTO_CUES_SOURCE",
    "BEATGRID_SOURCE",
    "AutoCueOut",
    "AutoCuesOut",
    "BeatgridFallbackOut",
    "FallbackBeatOut",
    "FallbackBeatgridOut",
    "get_auto_cues",
    "get_beatgrid_fallback",
    "router",
    "synthesize_fallback_beats",
]
