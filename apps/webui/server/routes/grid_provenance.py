"""Which analysis made a track's beatgrid, and its confidence (pin f85c5881).

`GET /api/v1/tracks/{stable_id}/grid-provenance` answers the two questions the
library's BPM hover could not: is this grid rekordbox's or our own analysis,
and how sure was the analyzer.

Fetched lazily on hover, never joined onto the list row. The source is one
selection read, but the confidence sits inside the canonical analysis
record's JSON: one row fetch and one parse per track, which a 200-row page
should not pay for a tooltip most rows never open.

Requirements (mini-PRD):
  - The source and its basis are resolved by the same function `/anlz` uses
    (`beatgrid_source_for_track`), so the hover can never name a different
    grid than the one the deck draws.
    [if] the hover says own while `/anlz` serves rekordbox [then] broken
  - Own source: backend, version, BPM and confidence come from the canonical
    beatgrid record, exactly as stored.
    [if] the confidence differs from the record's lane payload [then] broken
    [if] a missing or failed lane reports a number [then] broken
  - rekordbox source: confidence is null. rekordbox publishes none, and an own
    record's figure was not measured on rekordbox's grid.
    [if] a rekordbox-sourced track shows an own confidence [then] broken
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import get_read_state
from ..rb_vendor_pkg import own_beatgrid_overlay
from . import analysis as analysis_routes
from .rb_assets import _canonical_beatgrid_record
from .rb_assets_beatgrid_source import beatgrid_source_for_track

router = APIRouter(prefix="/tracks", tags=["rb-assets"])


class GridProvenanceOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    #: Whose grid the deck draws for this track.
    source: Literal["rekordbox", "own"]
    #: Why: the lane selection, or the per-track own default of an unmapped track.
    basis: Literal["selection", "unmapped-default"]
    #: Own lane state. None for rekordbox, whose grid is not judged here.
    status: Literal["ok", "failed", "missing"] | None
    reason: str | None
    backend: str | None
    backend_version: str | None
    bpm: float | None
    #: 0..1 as the analyzer stored it. None when there is no such measurement.
    bpm_confidence: float | None


@router.get("/{stable_id}/grid-provenance", response_model=GridProvenanceOut)
def get_track_grid_provenance(
    request: Request,
    response: Response,
    stable_id: str,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> GridProvenanceOut:
    # The answer moves with the lane selection and with every new own record,
    # neither of which a cache validator here could see.
    response.headers["Cache-Control"] = "no-store"
    has_rb_mapping = stable_id in rb_vendor.bulk_rb_meta([stable_id])
    source, basis = beatgrid_source_for_track(request, has_rb_mapping=has_rb_mapping)
    if source == own_beatgrid_overlay.SOURCE_REKORDBOX:
        return GridProvenanceOut(
            stable_id=stable_id, source="rekordbox", basis=basis, status=None,
            reason=None, backend=None, backend_version=None, bpm=None,
            bpm_confidence=None,
        )
    if source != own_beatgrid_overlay.SOURCE_OWN:
        raise RuntimeError(f"unhandled beatgrid source {source!r}")

    record = _canonical_beatgrid_record(
        analysis_routes._analysis_db_path(request), stable_id
    )
    if record is None:
        return GridProvenanceOut(
            stable_id=stable_id, source="own", basis=basis, status="missing",
            reason=own_beatgrid_overlay.OWN_BEATGRID_MISSING_REASON, backend=None,
            backend_version=None, bpm=None, bpm_confidence=None,
        )
    lane = record.lanes.get(own_beatgrid_overlay.OWN_BEATGRID_LANE)
    if lane is None:
        raise RuntimeError(
            f"canonical record {record.backend!r} for {stable_id!r} carries no beatgrid lane"
        )
    if lane.status != "ok":
        return GridProvenanceOut(
            stable_id=stable_id, source="own", basis=basis, status=lane.status,
            reason=lane.reason or f"own beatgrid lane {lane.status}",
            backend=record.backend, backend_version=record.backend_version,
            bpm=None, bpm_confidence=None,
        )
    return GridProvenanceOut(
        stable_id=stable_id, source="own", basis=basis, status="ok", reason=None,
        backend=record.backend, backend_version=record.backend_version,
        bpm=float(lane.payload["bpm"]),
        bpm_confidence=float(lane.payload["bpm_confidence"]),
    )
