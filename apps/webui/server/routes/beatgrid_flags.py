"""Beatgrid flags: which tracks Beat Sync may wander on (GRIDFLAG-03).

  GET  /api/v1/beatgrid-flags                 flagged tracks + their numbers
  GET  /api/v1/beatgrid-flags/scan            scan status
  POST /api/v1/beatgrid-flags/scan            bring stored verdicts up to date
  PUT  /api/v1/beatgrid-flags/{id}/dismissed  hide / restore one track's flag

The listing reads stored verdicts only (`grid_quality_store`); it never
parses a grid. Counts are over a NAMED denominator: `present` (tracks whose
audio is on this machine) by default, `all_tracks` on request. A track nobody
scanned, or with no readable grid, is `unknown` and is counted as such.

CLI twin: `opendj track grid-flags` and `opendj track grid-flag`.

-Claude
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from apps.adapters.rekordbox import config
from apps.adapters.rekordbox.errors import _open_ro
from apps.analysis_beatgrid.grid_quality import (
    FLAGGED_GRID_CLASSES,
    GRID_CLASSES,
    REASON_NOT_SCANNED,
    THRESHOLDS,
    GridClass,
    grid_quality_message,
    rule_version,
    unknown_quality,
)
from apps.shared.events import publish

from .. import grid_quality_scan, grid_quality_store
from ..backend import StateBackend
from ..deps import get_write_state
from ..etag import compute_etag
from ..grid_quality_scan import ScanScope

router = APIRouter(prefix="/beatgrid-flags", tags=["beatgrid-flags"])

GridClassFilter = Literal["flagged", "suspect", "variable_tempo", "unknown", "ok", "all"]
Availability = Literal["present", "all"]
DISMISS_FIELD = "grid_flag_dismissed"

#: Listing order: the grids most worth a look first, dismissed ones last.
_CLASS_RANK: dict[str, int] = {"suspect": 0, "variable_tempo": 1, "unknown": 2, "ok": 3}


class BeatgridFlagItem(BaseModel):
    stable_id: str
    title: str | None
    artist: str | None
    grid_class: GridClass
    reason: str | None
    dismissed: bool
    grid_source: str | None
    interval_count: int
    uneven_interval_count: int
    worst_deviation_ms: float
    worst_at_sec: float
    median_bpm: float | None
    tempo_marker_count: int | None
    steady_coverage: float | None
    steady_on_line: float | None
    steady_line_bpm: float | None
    computed_at: str | None
    message: str


class BeatgridFlagCounts(BaseModel):
    """Every count is over the response's `denominator`. The four classes sum
    to it; `dismissed` counts flagged tracks whose flag the user hid."""

    ok: int
    suspect: int
    variable_tempo: int
    unknown: int
    dismissed: int


class BeatgridFlagRule(BaseModel):
    version: str
    thresholds: dict[str, float | int]


class BeatgridScanStatus(BaseModel):
    state: Literal["idle", "running"]
    running_scope: ScanScope | None
    last_result: dict[str, Any] | None
    last_error: str | None


class BeatgridFlagsOut(BaseModel):
    denominator: Literal["present", "all_tracks"]
    #: Size of the denominator (kept under this name for both, see `denominator`).
    present: int
    counts: BeatgridFlagCounts
    unknown_reasons: dict[str, int]
    rule: BeatgridFlagRule
    scan: BeatgridScanStatus
    items: list[BeatgridFlagItem]


class DismissIn(BaseModel):
    dismissed: bool


class DismissOut(BaseModel):
    stable_id: str
    dismissed: bool
    etag: str


# --------------------------------------------------------------- helpers


def _track_facts(stable_ids: list[str]) -> dict[str, tuple[str | None, str | None, bool]]:
    """`stable_id -> (title, artist, dismissed)` in two chunked queries."""
    facts: dict[str, tuple[str | None, str | None, bool]] = {}
    conn = _open_ro(config.STATE_DB, "STATE_DB")
    try:
        chunk_size = 500
        for start in range(0, len(stable_ids), chunk_size):
            chunk = stable_ids[start : start + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            dismissed = {
                str(sid)
                for sid, value_json in conn.execute(
                    "SELECT stable_id, value_json FROM track_fields "
                    f"WHERE field_name = ? AND stable_id IN ({placeholders})",
                    (DISMISS_FIELD, *chunk),
                )
                if json.loads(value_json) is True
            }
            for sid, title, artists_json in conn.execute(
                "SELECT stable_id, title, artists_json FROM tracks "
                f"WHERE deleted_at IS NULL AND stable_id IN ({placeholders})",
                tuple(chunk),
            ):
                artists = json.loads(artists_json) if artists_json else []
                artist = ", ".join(str(a) for a in artists if a) if isinstance(artists, list) else str(artists)
                facts[str(sid)] = (title, artist or None, str(sid) in dismissed)
    finally:
        conn.close()
    return facts


def _matches(item: BeatgridFlagItem, grid_class: GridClassFilter, include_dismissed: bool) -> bool:
    if item.dismissed and not include_dismissed:
        return False
    if grid_class == "all":
        return True
    if grid_class == "flagged":
        return item.grid_class in FLAGGED_GRID_CLASSES
    return item.grid_class == grid_class


# ---------------------------------------------------------------- routes


@router.get("", response_model=BeatgridFlagsOut)
def list_beatgrid_flags(
    request: Request,
    grid_class: Annotated[GridClassFilter, Query()] = "flagged",
    include_dismissed: bool = Query(False),
    availability: Annotated[Availability, Query()] = "present",
    limit: int = Query(500, ge=1, le=20000),
) -> BeatgridFlagsOut:
    """Tracks by beatgrid class, with the numbers behind each verdict.

    `counts` cover the whole denominator whatever the filter and limit.
    """
    ids = grid_quality_scan.scope_ids(availability)
    stored = grid_quality_store.read_many(ids)
    facts = _track_facts(ids)
    counts = dict.fromkeys((*GRID_CLASSES, "dismissed"), 0)
    unknown_reasons: dict[str, int] = {}
    matched: list[BeatgridFlagItem] = []
    for stable_id in ids:
        row = stored.get(stable_id)
        quality = row.quality() if row is not None else unknown_quality(REASON_NOT_SCANNED)
        title, artist, dismissed = facts[stable_id]
        dismissed = dismissed and quality.grid_class in FLAGGED_GRID_CLASSES
        counts[quality.grid_class] += 1
        if dismissed:
            counts["dismissed"] += 1
        if quality.grid_class == "unknown" and quality.reason is not None:
            unknown_reasons[quality.reason] = unknown_reasons.get(quality.reason, 0) + 1
        item = BeatgridFlagItem(
            stable_id=stable_id,
            title=title,
            artist=artist,
            dismissed=dismissed,
            grid_source=row.grid_source if row is not None else None,
            computed_at=row.computed_at if row is not None else None,
            message=grid_quality_message(quality),
            **asdict(quality),
        )
        if _matches(item, grid_class, include_dismissed):
            matched.append(item)
    matched.sort(
        key=lambda item: (
            item.dismissed,
            _CLASS_RANK[item.grid_class],
            -item.uneven_interval_count,
            item.stable_id,
        )
    )
    return BeatgridFlagsOut(
        denominator="present" if availability == "present" else "all_tracks",
        present=len(ids),
        counts=BeatgridFlagCounts(**counts),
        unknown_reasons=dict(sorted(unknown_reasons.items())),
        rule=BeatgridFlagRule(version=rule_version(), thresholds=asdict(THRESHOLDS)),
        scan=BeatgridScanStatus(**grid_quality_scan.runner_for(request.app.state).status()),
        items=matched[:limit],
    )


@router.get("/scan", response_model=BeatgridScanStatus)
def get_scan_status(request: Request) -> BeatgridScanStatus:
    return BeatgridScanStatus(**grid_quality_scan.runner_for(request.app.state).status())


@router.post("/scan", response_model=BeatgridScanStatus)
def start_scan(
    request: Request,
    scope: Annotated[ScanScope, Query()] = "present",
    wait: bool = Query(False),
) -> BeatgridScanStatus:
    """Bring stored verdicts up to date. Incremental: an unchanged grid is
    skipped after one stat. `wait=true` returns when the scan has finished;
    otherwise it runs on a background thread and `GET /scan` reports it."""
    runner = grid_quality_scan.runner_for(request.app.state)
    if wait:
        return BeatgridScanStatus(**runner.run_blocking(scope))
    return BeatgridScanStatus(**runner.start(scope))


@router.put("/{stable_id}/dismissed", response_model=DismissOut)
def set_flag_dismissed(
    stable_id: str,
    body: DismissIn,
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> DismissOut:
    """Hide (or restore) one track's beatgrid flag. Stored as the user track
    field `grid_flag_dismissed` (source `webui`), the same path rating and
    comments take, so it carries provenance and travels with them."""
    current = backend.get_track(stable_id)  # NotFoundError -> 404 (app handler)
    updated = backend.update_track(
        stable_id,
        {DISMISS_FIELD: body.dismissed},
        expected_etag=compute_etag(current.stable_id, current.updated_at, current.selection_tag),
        source="webui",
    )
    publish("library.changed", {"kind": "tracks", "ids": [stable_id]})
    return DismissOut(
        stable_id=stable_id,
        dismissed=updated.grid_flag_dismissed,
        etag=compute_etag(updated.stable_id, updated.updated_at, updated.selection_tag),
    )
