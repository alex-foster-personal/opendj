"""HTTP trigger + status for vocal analysis -- issue #1038 / PARITY-08.

Vocal analysis (``apps.vocals``: scan, trickle, one, from-stems) was
CLI-only, so an agent driving the webui had no way to request it and the
UI had no control to surface it -- the agent-native parity hole this
router closes.

Deliberately a thin facade, no new job store:

  * ``POST /vocals/analyze`` classifies every requested track with the
    SAME categories :func:`apps.vocals.cli.classify` already assigns
    (``missing_analysis``, ``missing_file``, ``pvdi``, ``cached_demucs``)
    and only enqueues a track in the ``todo`` category, through the
    existing claim/lease path (``mode: "one"``, real demucs) or the
    existing stems-derive path (``mode: "from-stems"``, CPU only). A
    track outside ``todo`` is refused with that same category, not
    silently skipped.
  * ``GET /vocals/{stable_id}/status`` reuses the exact vocal-cache
    reader ``/anlz`` already serves through
    ``rb_vendor.vocals_for_content`` (``rb_assets.py``'s ``vocals`` field),
    so the two never disagree about what "analyzed" means for a track.

Requirements (mini-PRD):
  ✔︎ ✅ POST /vocals/analyze: enqueue by stable_ids or playlist_id.
    [if] a track has a stem bundle and mode is from-stems [then] a
    vocal-cache entry appears and it is reported claimed
    [if] a track has no local ANLZ .DAT [then ⛔️] refused missing_analysis,
    nothing enqueued
    [if] neither stable_ids nor playlist_id is given, or both are [then ⛔️] 422
  ✔︎ ✅ GET /vocals/{stable_id}/status: the same four vocals states /anlz serves.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from apps.shared.paths import DATA_DIR
from apps.stems.artifacts import StemArtifactError, StemBundleNotFoundError
from apps.vocals import cache as vcache
from apps.vocals import from_stems as vfrom_stems
from apps.vocals.cli import (
    CATEGORY_TODO,
    WORKER_TIMEOUT_S,
    Ctx,
    TrackClaim,
    VocalTrack,
    _claim_track,
    _managed_track_claim,
    _process_one,
    classify,
    load_tracks,
)

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import get_read_state, get_write_state

router = APIRouter(prefix="/vocals", tags=["vocals"])

# Refusal categories not already covered by apps.vocals.cli's CATEGORY_*
# constants: those classify a track by its EXISTING analysis state, these
# by what THIS request could not do with it.
CATEGORY_UNMAPPED: str = "unmapped"
CATEGORY_MISSING_STEMS: str = "missing_stems"
CATEGORY_IN_PROGRESS: str = "in_progress"


class VocalsAnalyzeIn(BaseModel):
    stable_ids: list[str] | None = Field(default=None, min_length=1)
    playlist_id: str | None = None
    mode: Literal["from-stems", "one"]

    @model_validator(mode="after")
    def _exactly_one_source(self) -> VocalsAnalyzeIn:
        if (self.stable_ids is None) == (self.playlist_id is None):
            raise ValueError("exactly one of stable_ids or playlist_id is required")
        return self


class VocalsAnalyzeOut(BaseModel):
    claimed: list[str]
    refused: dict[str, str]


class VocalsRegionOut(BaseModel):
    start_s: float
    end_s: float
    intensity: int
    confidence: float | None = None


class VocalsStatusOut(BaseModel):
    """Same shape and four states as ``/anlz``'s ``vocals`` field."""

    stable_id: str
    status: Literal["rekordbox", "no_vocals", "demucs", "not_analyzed"]
    fps: float | None = None
    regions: list[VocalsRegionOut] = Field(default_factory=list)


def _resolve_stable_ids(body: VocalsAnalyzeIn, backend: StateBackend) -> list[str]:
    if body.stable_ids is not None:
        return list(body.stable_ids)
    assert body.playlist_id is not None  # enforced by _exactly_one_source
    playlist = backend.get_playlist(body.playlist_id)  # 404s via NotFoundError
    return list(playlist.items)


def _unique_stable_ids(stable_ids: list[str]) -> list[str]:
    """Preserve request order while preventing duplicate worker execution."""
    seen: set[str] = set()
    unique: list[str] = []
    for stable_id in stable_ids:
        if stable_id in seen:
            continue
        seen.add(stable_id)
        unique.append(stable_id)
    return unique


def _process_claimed_track(ctx: Ctx, track: VocalTrack, claim: TrackClaim) -> None:
    """Run a claimed Demucs job after its HTTP response has been sent."""
    with _managed_track_claim(claim):
        _process_one(ctx, track, "[http]", WORKER_TIMEOUT_S, claim)


@router.post("/analyze", response_model=VocalsAnalyzeOut, status_code=202)
def analyze_vocals(
    body: VocalsAnalyzeIn,
    request: Request,
    background_tasks: BackgroundTasks,
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> VocalsAnalyzeOut:
    stable_ids = _unique_stable_ids(_resolve_stable_ids(body, backend))
    ctx = Ctx(data_dir=DATA_DIR)
    by_id = {t.stable_id: t for t in load_tracks(ctx, None)}

    claimed: list[str] = []
    refused: dict[str, str] = {}
    candidates: list[VocalTrack] = []
    for sid in stable_ids:
        tr = by_id.get(sid)
        if tr is None:
            refused[sid] = CATEGORY_UNMAPPED
            continue
        candidates.append(tr)

    classify(ctx, candidates)
    for tr in candidates:
        if tr.category != CATEGORY_TODO:
            refused[tr.stable_id] = tr.category
            continue
        assert tr.audio_path is not None  # CATEGORY_TODO implies audio_on_disk

        if body.mode == "from-stems":
            configured_roots = getattr(request.app.state, "stem_roots", None)
            roots = (
                tuple(Path(root) for root in configured_roots)
                if configured_roots is not None
                else None
            )
            try:
                vfrom_stems.write_from_bundle(
                    ctx.data_dir,
                    tr.stable_id,
                    tr.audio_path,
                    stem_roots=roots,
                )
            except (StemBundleNotFoundError, StemArtifactError):
                refused[tr.stable_id] = CATEGORY_MISSING_STEMS
                continue
            claimed.append(tr.stable_id)
        else:  # mode == "one"
            cache_file = vcache.cache_path(ctx.data_dir, tr.stable_id)
            claim = _claim_track(cache_file)
            if claim is None:
                refused[tr.stable_id] = CATEGORY_IN_PROGRESS
                continue
            background_tasks.add_task(_process_claimed_track, ctx, tr, claim)
            claimed.append(tr.stable_id)

    return VocalsAnalyzeOut(claimed=claimed, refused=refused)


@router.get("/{stable_id}/status", response_model=VocalsStatusOut)
def get_vocal_status(
    stable_id: str,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> VocalsStatusOut:
    try:
        content = rb_vendor.resolve_content(stable_id)
    except HTTPException as exc:
        detail: dict[str, str] = exc.detail if isinstance(exc.detail, dict) else {}
        if detail.get("code") != "VENDOR_MAPPING_NOT_FOUND":
            raise
        # No rekordbox mapping: no PVDI is possible and the local-import
        # demucs merge is a separate, not-yet-shipped gap (PARITY-TODO "Merge
        # the demucs vocal-cache into empty_anlz_payload"), same as /anlz.
        return VocalsStatusOut(stable_id=stable_id, status="not_analyzed")
    vocals = rb_vendor.vocals_for_content(content)
    return VocalsStatusOut(
        stable_id=stable_id,
        status=vocals["status"],
        fps=vocals.get("fps"),
        regions=[VocalsRegionOut(**r) for r in vocals.get("regions", [])],
    )


__all__ = [
    "CATEGORY_IN_PROGRESS",
    "CATEGORY_MISSING_STEMS",
    "CATEGORY_UNMAPPED",
    "VocalsAnalyzeIn",
    "VocalsAnalyzeOut",
    "VocalsRegionOut",
    "VocalsStatusOut",
    "analyze_vocals",
    "get_vocal_status",
    "router",
]
