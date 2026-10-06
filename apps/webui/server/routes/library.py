"""Library-wide views that are not a single track/playlist -- LIBUX-06, READY-01."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from apps.library_wheel.query import AXES, LibraryWheelError, query_library_wheel
from apps.shared import fd_anchored_walk, platform_paths
from apps.shared import paths as shared_paths
from apps.shared.state.db import open_ro
from apps.stems.artifacts import DEFAULT_STEMS_DIR, stem_roots
from apps.webui.server.library_readiness import (
    LibraryReadinessOut,
    ReadinessAxis,
    query_library_readiness,
)
from apps.webui.server import rb_vendor
from apps.webui.server.backend import StateBackend
from apps.webui.server.deps import get_library_data_dir, get_read_state
from apps.webui.server.models import RowAssetOut, RowAssetsOut
from apps.webui.server.rb_vendor_pkg.track_rows import bulk_preview_strips

router = APIRouter(prefix="/library", tags=["library"])

_AxisKey = Literal[
    "genre", "decade", "play_count", "popularity", "overplayed_ness", "playlist", "set_played_in"
]

MAX_READINESS_ITEMS: int = 1000
MAX_PREVIEW_STRIP_IDS: int = 200
DEFAULT_READINESS_ITEMS: int = 200


def _stem_roots(request: Request) -> tuple[Path, ...]:
    configured = getattr(request.app.state, "stem_roots", None)
    if configured is not None:
        return tuple(Path(root) for root in configured)
    return stem_roots(DEFAULT_STEMS_DIR)


class ShareRootReanchorOut(BaseModel):
    """Answer of ``POST /library/share-root/reanchor``."""

    exists: bool


@router.post("/share-root/reanchor", response_model=ShareRootReanchorOut)
def reanchor_share_root() -> ShareRootReanchorOut:
    """Trust the rekordbox share root as it is now, and forget what was read under it.

    The engine remembers which directory the share root was when it first
    read it, and refuses to read below a root that has since become a
    different one (LIBM-137): a volume mounted again at the same path, a
    directory swapped in by rename, a symlinked share root pointed somewhere
    else. Nothing re-trusts it on its own, because each of those is also what
    an attack looks like. This call is how the root's owner says the new
    directory is intended; it records that directory's identity and empties
    the listing's row memory.

    ``exists`` is false, and nothing changes, when the share root is not there.
    The call is refused with 409, and ``detail`` says why, when a directory
    ABOVE the share root is a symlink: only the share root itself may be one.
    It is 501 on a platform with no anchored walk (Windows), where the share
    root is never anchored and so there is nothing to re-trust.
    """
    try:
        exists = platform_paths.reanchor_share_root()
    except fd_anchored_walk.RootReanchorUnavailable as unavailable:
        raise HTTPException(status_code=501, detail=f"share root not re-anchored: {unavailable}") from unavailable
    except fd_anchored_walk.RootReanchorRefused as refusal:
        raise HTTPException(status_code=409, detail=f"share root not re-anchored: {refusal}") from refusal
    return ShareRootReanchorOut(exists=exists)


class PreviewStripsIn(BaseModel):
    """Body of ``POST /library/preview-strips``."""

    ids: list[str] = Field(min_length=1, max_length=MAX_PREVIEW_STRIP_IDS)


class PreviewStripOut(BaseModel):
    preview_b64: str
    preview_max: int


class PreviewStripsOut(BaseModel):
    """``strips`` has every asked id: its strip, or null when none is on disk yet.

    ``pending`` lists the null ids the ahead-analysis drain was bumped for and
    will write, so asking again later can fill them. A null id not in
    ``pending`` stays null until something else analyzes it.
    """

    strips: dict[str, PreviewStripOut | None]
    pending: list[str]


@router.post("/preview-strips", response_model=PreviewStripsOut)
def post_preview_strips(body: PreviewStripsIn, request: Request) -> PreviewStripsOut:
    """Re-read the Preview strip of up to 200 rows a listing already returned (NATIVE-21).

    Same reads as a listing row, never a decode. A long-lived page's rows
    keep the strip they were listed with; this is how rows in view catch up
    with strips written since, without a click or a full re-list.
    """
    found = bulk_preview_strips(body.ids)
    drain = getattr(request.app.state, "ahead_analysis", None)
    pending: list[str] = []
    if drain is not None:
        pending = [sid for sid, strip in found.strips.items() if strip is None and sid in found.unmapped]
        for sid in pending:
            drain.bump(sid)
    return PreviewStripsOut(
        strips={
            sid: None if strip is None else PreviewStripOut(preview_b64=strip[0], preview_max=strip[1])
            for sid, strip in found.strips.items()
        },
        pending=pending,
    )


@router.post("/row-assets", response_model=RowAssetsOut, operation_id="post_row_assets")
def post_row_assets(
    body: PreviewStripsIn,
    request: Request,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
    data_dir: Path = Depends(get_library_data_dir),  # noqa: B008  # FastAPI DI
) -> RowAssetsOut:
    """The per-row disk reads the library index leaves out, for up to 200 rows (LIBM-172).

    Preview strip, vocal regions and cover verdict, read exactly as a ``GET /tracks``
    row reads them. The browser asks for the rows in view only, so a 9,713-row index
    never pays these reads up front. An id that is not a library track is absent.
    Strip-less unmapped ids bump the ahead-analysis drain, as ``/preview-strips`` does.
    """
    found = backend.get_tracks_bulk(body.ids)
    tracks = [found[sid] for sid in dict.fromkeys(body.ids) if sid in found]
    rows = rb_vendor.build_track_rows(tracks, data_dir=data_dir)
    drain = getattr(request.app.state, "ahead_analysis", None)
    pending = [r["stable_id"] for r in rows if r["preview_b64"] is None and not r["has_rb_mapping"]]
    if drain is not None:
        for sid in pending:
            drain.bump(sid)
    return RowAssetsOut(
        assets={
            r["stable_id"]: RowAssetOut(
                preview_b64=r["preview_b64"],
                preview_max=r["preview_max"],
                vocals=r["vocals"],
                artwork_available=r["artwork_available"],
                artwork_status=r["artwork_status"],
            )
            for r in rows
        },
        pending=pending if drain is not None else [],
    )


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
