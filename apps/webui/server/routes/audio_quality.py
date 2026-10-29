"""Audio quality (QLT): the letter per track, the scan, and one track's facts (QLTSCAN-04).

  GET  /api/v1/audio-quality                 letters, counts and issues over present tracks
  GET  /api/v1/audio-quality/scan            scan status
  POST /api/v1/audio-quality/scan            bring stored facts up to date
  POST /api/v1/audio-quality/scan/stop       end a running scan after the files in flight
  GET  /api/v1/audio-quality/{stable_id}     one track's letter, hover text and measured facts

Reads stored facts only (`audio_quality_store`); nothing here decodes audio
except the scan. Counts are over a NAMED denominator: tracks whose audio is
on this machine. A track nobody scanned has its bitrate letter with
`checked: false`, and is counted under `unchecked`, never as verified.

CLI twin: `opendj track quality`, `opendj track quality-scan`,
`opendj track quality-of <stable_id>`.

-Claude
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from apps.adapters.rekordbox.errors import _open_ro
from apps.shared import audio_grade, audio_quality
from apps.shared.audio_scan import THRESHOLDS as SCAN_THRESHOLDS
from apps.shared.audio_scan import rule_version
from apps.shared.state.locations import local_audio_path

from .. import audio_quality_scan, audio_quality_store
from ..audio_quality_scan import MAX_WORKERS
from ..deps import get_library_state_db

router = APIRouter(prefix="/audio-quality", tags=["audio-quality"])

LetterFilter = Literal["all", "A", "B", "C", "D", "unchecked", "issues", "failed"]


class AudioQualityItem(BaseModel):
    stable_id: str
    title: str | None
    file_path: str
    letter: Literal["A", "B", "C", "D"] | None
    checked: bool
    meaning: str
    issues: list[str]
    hover: str
    cutoff_hz: int | None
    scan_error: str | None


class AudioQualityCounts(BaseModel):
    """Over the `present` denominator. A-D count checked letters only."""

    A: int
    B: int
    C: int
    D: int
    unchecked: int
    unknown: int
    failed: int
    with_issues: int


class AudioQualityRule(BaseModel):
    scan_version: str
    grade_version: str
    scan_thresholds: dict[str, Any]
    grade_thresholds: dict[str, int]


class AudioQualityScanStatus(BaseModel):
    state: Literal["idle", "running"]
    stopping: bool
    last_result: dict[str, Any] | None
    last_error: str | None


class AudioQualityOut(BaseModel):
    denominator: Literal["present"]
    present: int
    counts: AudioQualityCounts
    issue_counts: dict[str, int]
    rule: AudioQualityRule
    scan: AudioQualityScanStatus
    items: list[AudioQualityItem]


class AudioQualityTrackOut(BaseModel):
    stable_id: str
    file_path: str
    quality: dict[str, Any]
    facts: dict[str, Any] | None
    scan_error: str | None
    computed_at: str | None


# --------------------------------------------------------------- helpers


def _track_facts(stable_ids: list[str], state_db: Path) -> dict[str, tuple[str | None, int | None]]:
    """`stable_id -> (title, duration_ms)` in chunked queries."""
    facts: dict[str, tuple[str | None, int | None]] = {}
    conn = _open_ro(state_db, "STATE_DB")
    try:
        for start in range(0, len(stable_ids), 500):
            chunk = stable_ids[start : start + 500]
            placeholders = ",".join("?" * len(chunk))
            for sid, title, duration_ms in conn.execute(
                "SELECT stable_id, title, duration_ms FROM tracks "
                f"WHERE deleted_at IS NULL AND stable_id IN ({placeholders})",
                tuple(chunk),
            ):
                facts[str(sid)] = (title, duration_ms)
    finally:
        conn.close()
    return facts


def _item(
    stable_id: str, path: str, title: str | None, duration_ms: int | None,
    stored: audio_quality_store.StoredScan | None,
) -> AudioQualityItem:
    quality = audio_quality.classify(path, duration_ms).as_dict()
    facts, error = _current(stored, path)
    grade = audio_grade.grade(quality, facts, error)
    return AudioQualityItem(
        stable_id=stable_id, title=title, file_path=path, letter=grade.letter,
        checked=grade.checked, meaning=grade.meaning, issues=list(grade.issues),
        hover="\n".join(grade.hover), cutoff_hz=grade.cutoff_hz, scan_error=error,
    )


def _current(
    stored: audio_quality_store.StoredScan | None, path: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Stored facts checked against the file's full identity (path, mtime, size)."""
    identity = audio_quality_scan.file_identity(path)
    if isinstance(identity, str):
        return None, identity
    return audio_quality_store.current_facts(stored, identity[1], identity[0])


def _matches(item: AudioQualityItem, letter: LetterFilter) -> bool:
    if letter == "all":
        return True
    if letter == "unchecked":
        return not item.checked
    if letter == "issues":
        return bool(item.issues)
    if letter == "failed":
        return item.scan_error is not None
    return item.checked and item.letter == letter


def _status(request: Request) -> AudioQualityScanStatus:
    return AudioQualityScanStatus(**audio_quality_scan.runner_for(request.app.state).status())


# ---------------------------------------------------------------- routes


@router.get("", response_model=AudioQualityOut)
def list_audio_quality(
    request: Request,
    letter: Annotated[LetterFilter, Query()] = "all",
    limit: int | None = Query(None, ge=1),
    state_db: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> AudioQualityOut:
    """Letters over every present track, worst first. `counts` and
    `issue_counts` cover the whole denominator whatever the filter. Without
    `limit` every matching track is listed; `limit` keeps the worst N."""
    tracks = audio_quality_scan.present_tracks(state_db)
    ids = [sid for sid, _path in tracks]
    stored = audio_quality_store.read_many(ids, state_db)
    meta = _track_facts(ids, state_db)
    counts = dict.fromkeys(("A", "B", "C", "D", "unchecked", "unknown", "failed", "with_issues"), 0)
    issue_counts: dict[str, int] = {}
    items: list[AudioQualityItem] = []
    for sid, path in tracks:
        title, duration_ms = meta.get(sid, (None, None))
        item = _item(sid, path, title, duration_ms, stored.get(sid))
        if item.letter is None:
            counts["unknown"] += 1
        elif item.checked:
            counts[item.letter] += 1
        else:
            counts["unchecked"] += 1
        counts["failed"] += item.scan_error is not None
        counts["with_issues"] += bool(item.issues)
        for key in item.issues:
            issue_counts[key] = issue_counts.get(key, 0) + 1
        if _matches(item, letter):
            items.append(item)
    items.sort(key=lambda it: ("DCBA".find(it.letter) if it.letter else 9, not it.checked, it.stable_id))
    return AudioQualityOut(
        denominator="present",
        present=len(tracks),
        counts=AudioQualityCounts(**counts),
        issue_counts=dict(sorted(issue_counts.items())),
        rule=AudioQualityRule(
            scan_version=rule_version(),
            grade_version=audio_grade.grade_rule_version(),
            scan_thresholds=asdict(SCAN_THRESHOLDS),
            grade_thresholds=asdict(audio_grade.THRESHOLDS),
        ),
        scan=_status(request),
        items=items if limit is None else items[:limit],
    )


@router.get("/scan", response_model=AudioQualityScanStatus)
def get_scan_status(request: Request) -> AudioQualityScanStatus:
    return _status(request)


@router.post("/scan", response_model=AudioQualityScanStatus)
def start_scan(
    request: Request,
    wait: bool = Query(False),
    workers: int = Query(1, ge=1, le=MAX_WORKERS),
    limit: int | None = Query(None, ge=1),
    state_db: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> AudioQualityScanStatus:
    """Bring stored facts up to date. Incremental and resumable: unchanged
    files are skipped after one stat. Without `wait` it runs in the
    background with one worker; `wait=true` runs here with `workers`."""
    runner = audio_quality_scan.runner_for(request.app.state)
    if wait:
        return AudioQualityScanStatus(**runner.run_blocking(state_db, workers, limit))
    return AudioQualityScanStatus(**runner.start(state_db, limit))


@router.post("/scan/stop", response_model=AudioQualityScanStatus)
def stop_scan(request: Request) -> AudioQualityScanStatus:
    """End a running scan after the files in flight; a later scan resumes."""
    return AudioQualityScanStatus(**audio_quality_scan.runner_for(request.app.state).stop())


def _local_path(stable_id: str, state_db: Path) -> str | None:
    """This one track's local audio path, the listing's predicate, no library walk."""
    conn = _open_ro(state_db, "STATE_DB")
    try:
        found = local_audio_path(conn, stable_id)
    finally:
        conn.close()
    return str(found) if found is not None else None


@router.get("/{stable_id}", response_model=AudioQualityTrackOut)
def get_track_quality(
    stable_id: str,
    state_db: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> AudioQualityTrackOut:
    """One present track: its letter and hover (in `quality.grade`) and the
    measured facts behind them. 404 when its audio is not on this machine."""
    path = _local_path(stable_id, state_db)
    if path is None:
        raise HTTPException(status_code=404, detail=f"no local audio for track {stable_id!r}")
    _title, duration_ms = _track_facts([stable_id], state_db).get(stable_id, (None, None))
    stored = audio_quality_store.read_many([stable_id], state_db).get(stable_id)
    quality = audio_quality.classify(path, duration_ms).as_dict()
    facts, error = _current(stored, path)
    quality["grade"] = audio_grade.grade(quality, facts, error).as_dict()
    return AudioQualityTrackOut(
        stable_id=stable_id, file_path=path, quality=quality, facts=facts,
        scan_error=error,
        computed_at=stored.computed_at if stored is not None else None,
    )
