"""HTTP surface for Rekordbox <-> djay Pro library sync (SYNC-01..06).

Mounted at ``/api/v1/rb-djay-sync``. Distinct from CloudSync hub routes
under ``/api/v1/sync/*`` and from smartlist one-way writeback.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from apps.shared.rekordbox_writeback import RekordboxWritebackDisabled, require_writeback_enabled
from apps.sync.djay_sync_service import (
    RbDjaySyncError,
    get_status,
    list_djay_playlists,
    run_analysis_apply,
    run_cues_apply,
    run_cues_plan,
    run_match,
    run_metadata_plan,
    run_playlist_apply,
    run_playlist_plan,
    run_ratings_apply,
)

router = APIRouter(prefix="/rb-djay-sync", tags=["rb-djay-sync"])


def _http_error(exc: RbDjaySyncError) -> HTTPException:
    status = 422
    if exc.code == "RB_DJAY_DB_MISSING":
        status = 404
    elif exc.code == "WRITEBACK_DISABLED":
        status = 403
    elif exc.code == "FINGERPRINT_UNAVAILABLE":
        status = 503
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


class MatchRequest(BaseModel):
    use_fingerprint: bool = False
    out_dir: str | None = None


class PlaylistPlanRequest(BaseModel):
    matches_path: str | None = None
    only_playlists: list[str] | None = None
    max_ops: int = 10_000


class PlaylistApplyRequest(BaseModel):
    dry_run: bool = True
    plan_path: str | None = None
    playlists: list[str] | None = None
    bulk: bool = False
    live: bool = False
    i_understand_the_risks: bool = False
    allow_broken: bool = False


class MetadataPlanRequest(BaseModel):
    matches_path: str | None = None
    min_confidence: float = 0.70
    prefer: Literal["rekordbox", "djay", "newest"] = "newest"
    include_cues: bool = False


class CuesPlanRequest(BaseModel):
    matches_path: str | None = None
    min_confidence: float = 0.70


class CuesApplyRequest(BaseModel):
    dry_run: bool = True
    diff_csv: str | None = None
    live: bool = False
    cautious: bool = False
    bulk: bool = False
    tracks: list[str] | None = None
    i_understand_the_risks: bool = False


class AnalysisApplyRequest(BaseModel):
    dry_run: bool = True
    diff_csv: str | None = None
    live: bool = False
    fields: list[str] | None = None
    i_understand_the_risks: bool = False


class RatingsApplyRequest(BaseModel):
    dry_run: bool = True
    diff_csv: str | None = None
    live: bool = False
    cautious: bool = False
    bulk: bool = False
    tracks: list[str] | None = None
    i_understand_the_risks: bool = False


@router.get("/status")
def rb_djay_sync_status() -> dict:
    """SYNC-01..06 capability probe: DB paths, artefacts, fingerprint, writeback."""
    return get_status()


@router.get("/djay/playlists")
def rb_djay_sync_djay_playlists() -> dict:
    """SYNC-01: enumerate djay playlists and track UUID memberships."""
    try:
        playlists = list_djay_playlists()
    except RbDjaySyncError as exc:
        raise _http_error(exc) from exc
    return {"playlists": playlists, "count": len(playlists)}


@router.post("/match")
def rb_djay_sync_match(body: MatchRequest) -> dict:
    """SYNC-02: build matches.csv from working-copy vendor DBs."""
    try:
        return run_match(
            out_dir=Path(body.out_dir) if body.out_dir else None,
            use_fingerprint=body.use_fingerprint,
        )
    except RbDjaySyncError as exc:
        raise _http_error(exc) from exc


@router.post("/playlists/plan")
def rb_djay_sync_playlists_plan(body: PlaylistPlanRequest) -> dict:
    """SYNC-03: compute playlist-plan.json and companion artefacts."""
    try:
        return run_playlist_plan(
            matches_path=Path(body.matches_path) if body.matches_path else None,
            only_playlists=body.only_playlists,
            max_ops=body.max_ops,
        )
    except RbDjaySyncError as exc:
        raise _http_error(exc) from exc


@router.post("/playlists/apply")
def rb_djay_sync_playlists_apply(body: PlaylistApplyRequest) -> dict:
    """SYNC-03: dry-run or live apply of playlist-plan.json into djay."""
    try:
        return run_playlist_apply(
            dry_run=body.dry_run,
            plan_path=Path(body.plan_path) if body.plan_path else None,
            playlists=body.playlists,
            bulk=body.bulk,
            live=body.live,
            i_understand_the_risks=body.i_understand_the_risks,
            allow_broken=body.allow_broken,
        )
    except RbDjaySyncError as exc:
        raise _http_error(exc) from exc
    except RekordboxWritebackDisabled as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": exc.code, "message": exc.message, "surface": exc.surface_id},
        ) from exc


@router.post("/metadata/plan")
def rb_djay_sync_metadata_plan(body: MetadataPlanRequest) -> dict:
    """SYNC-05 + SYNC-06 (+ optional SYNC-04 cues) diff CSV generation."""
    try:
        return run_metadata_plan(
            matches_path=Path(body.matches_path) if body.matches_path else None,
            min_confidence=body.min_confidence,
            prefer=body.prefer,
            include_cues=body.include_cues,
        )
    except RbDjaySyncError as exc:
        raise _http_error(exc) from exc


@router.post("/cues/plan")
def rb_djay_sync_cues_plan(body: CuesPlanRequest) -> dict:
    """SYNC-04: cue comparison CSV."""
    try:
        return run_cues_plan(
            matches_path=Path(body.matches_path) if body.matches_path else None,
            min_confidence=body.min_confidence,
        )
    except RbDjaySyncError as exc:
        raise _http_error(exc) from exc


@router.post("/cues/apply")
def rb_djay_sync_cues_apply(body: CuesApplyRequest) -> dict:
    """SYNC-04: dry-run or scaffold live cue apply."""
    if body.live:
        require_writeback_enabled("http.rb_djay_sync.cues.apply")
    try:
        return run_cues_apply(
            dry_run=body.dry_run,
            diff_csv=Path(body.diff_csv) if body.diff_csv else None,
            live=body.live,
            cautious=body.cautious,
            bulk=body.bulk,
            tracks=body.tracks,
            i_understand_the_risks=body.i_understand_the_risks,
        )
    except RbDjaySyncError as exc:
        raise _http_error(exc) from exc


@router.post("/analysis/apply")
def rb_djay_sync_analysis_apply(body: AnalysisApplyRequest) -> dict:
    """SYNC-05: dry-run or live analysis-field apply from analysis-diff.csv."""
    if body.live:
        require_writeback_enabled("http.rb_djay_sync.analysis.apply")
    try:
        return run_analysis_apply(
            dry_run=body.dry_run,
            diff_csv=Path(body.diff_csv) if body.diff_csv else None,
            live=body.live,
            fields=body.fields,
            i_understand_the_risks=body.i_understand_the_risks,
        )
    except RbDjaySyncError as exc:
        raise _http_error(exc) from exc


@router.post("/ratings/apply")
def rb_djay_sync_ratings_apply(body: RatingsApplyRequest) -> dict:
    """SYNC-06: dry-run or live ratings apply from ratings-diff.csv."""
    if body.live:
        require_writeback_enabled("http.rb_djay_sync.ratings.apply")
    try:
        return run_ratings_apply(
            dry_run=body.dry_run,
            diff_csv=Path(body.diff_csv) if body.diff_csv else None,
            live=body.live,
            cautious=body.cautious,
            bulk=body.bulk,
            tracks=body.tracks,
            i_understand_the_risks=body.i_understand_the_risks,
        )
    except RbDjaySyncError as exc:
        raise _http_error(exc) from exc
