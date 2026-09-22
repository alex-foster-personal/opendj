"""Library-wide views that are not a single track/playlist -- LIBUX-06, READY-01."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from apps.library_wheel.query import AXES, LibraryWheelError, query_library_wheel
from apps.shared import paths as shared_paths
from apps.shared.state.db import open_ro
from apps.stems.artifacts import DEFAULT_STEMS_DIR, stem_roots
from apps.webui.server.library_readiness import (
    LibraryReadinessOut,
    ReadinessAxis,
    query_library_readiness,
)

router = APIRouter(prefix="/library", tags=["library"])

_AxisKey = Literal[
    "genre", "decade", "play_count", "popularity", "overplayed_ness", "playlist", "set_played_in"
]

MAX_READINESS_ITEMS: int = 1000
DEFAULT_READINESS_ITEMS: int = 200


def _stem_roots(request: Request) -> tuple[Path, ...]:
    configured = getattr(request.app.state, "stem_roots", None)
    if configured is not None:
        return tuple(Path(root) for root in configured)
    return stem_roots(DEFAULT_STEMS_DIR)


@router.get("/wheel")
def get_library_wheel(
    axis: Annotated[_AxisKey, Query(
        description=(
            "Per-track overlay on the genre-grouped tree. A disabled axis "
            "(decade, overplayed_ness, set_played_in) still returns the "
            "real tree with every axis_value null and a stated reason, "
            "never a fabricated number."
        ),
    )] = "play_count",
) -> dict[str, object]:
    try:
        return query_library_wheel(
            shared_paths.STATE_DB, shared_paths.REKORDBOX_PLAIN_DB, axis=axis
        )
    except LibraryWheelError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "LIBRARY_WHEEL_UNAVAILABLE", "message": str(exc)},
        ) from exc


@router.get("/wheel/axes")
def get_library_wheel_axes() -> list[dict[str, object]]:
    """Axis catalog alone, so a picker can render enabled/disabled without
    paying for a full tree fetch."""
    return [
        {"key": a.key, "label": a.label, "enabled": a.enabled, "reason": a.reason}
        for a in AXES
    ]


@router.get("/readiness", response_model=LibraryReadinessOut)
def get_library_readiness(
    request: Request,
    limit: Annotated[int, Query(
        ge=1,
        le=MAX_READINESS_ITEMS,
        description="Cap on listed items. Never caps the reported counts.",
    )] = DEFAULT_READINESS_ITEMS,
    axis: Annotated[ReadinessAxis, Query(
        description="Which present tracks appear in items.",
    )] = "not_ready",
) -> LibraryReadinessOut:
    """Per-present-track readiness across beatgrid, waveform, stems, analysis, sync.

    Requirements (mini-PRD):
      READY-01: GET /api/v1/library/readiness reports per-present-track and
      aggregate readiness. Counts use the present (materialized local audio)
      denominator, never all tracks rows.
        [if] a track's file is not materialized [then] it is absent from
        present, items, and every count
        [if] a present track has no PQTZ, no own beatgrid lane ok, and no
        usable downbeats_s [then] counts.beatgrid_missing includes it and an
        axis=beatgrid item names it
        [if] a present track has a stems directory that load_stem_bundle
        rejects and no valid bundle in any configured root [then]
        stems == corrupt on the item and both counts.stems_corrupt and
        /ingest/coverage corrupt.stems are >= 1
        [if] limit is 1 and many present tracks are not ready [then] items
        has 1 row and counts.not_ready is still the full population
        [if] a present track has a validateBeatGrid-passing grid [then]
        sync_compatible is true without anyone engaging Beat Sync
    """
    return query_library_readiness(
        open_ro, _stem_roots(request), limit=limit, axis=axis
    )


__all__ = ["router"]
