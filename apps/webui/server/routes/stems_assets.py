"""CloudSync stem bundle push/hydrate routes (agent-native parity, D13.3).

Sibling of ``routes/stems.py``, which serves validated local bundles under
``/tracks/{stable_id}/stems``. These endpoints drive the migration rail and
the hash-based hydrate path without going through the track-scoped loader.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from apps.lyrics import stems_sync

router = APIRouter(tags=["stems"])


class StemBulkHydrateIn(BaseModel):
    """Agent-native parity body for ``python -m apps.stems bulk-hydrate``."""

    model_config = ConfigDict(frozen=True)

    stable_ids: list[str] | None = None
    playlist: str | None = None
    budget_bytes: int = Field(gt=0)
    include_reserved: bool = False
    refresh_index: bool = False
    data_dir: str | None = None


class StemBulkHydrateOutcomeOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    status: str
    bytes_fetched: int = 0
    reason: str | None = None


class StemBulkHydrateOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    fetched: list[StemBulkHydrateOutcomeOut]
    skipped: list[StemBulkHydrateOutcomeOut]
    bytes_fetched: int


class StemIndexBuildIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    journal_path: str | None = None
    publish: bool = False
    data_dir: str | None = None


class StemIndexBuildOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    bundles: int
    files: int
    published: bool


class StemHydrateIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest_path: str
    dry_run: bool = False
    data_dir: str | None = None


class StemPushMissingIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    dry_run: bool = False
    data_dir: str | None = Field(default=None)


@router.post("/stems/{stable_id}/hydrate")
def hydrate_stem(stable_id: str, body: StemHydrateIn) -> dict[str, str]:
    data_dir = Path(body.data_dir) if body.data_dir else None
    try:
        rc = stems_sync.hydrate(
            stable_id,
            manifest_path=Path(body.manifest_path),
            data_dir=data_dir,
            dry_run=body.dry_run,
        )
    except (ValueError, Exception) as exc:
        from apps.cloud.eviction import HydrationError

        if isinstance(exc, (HydrationError, ValueError)):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise
    if rc != 0:
        raise HTTPException(status_code=500, detail="hydrate failed")
    return {"status": "ok", "stable_id": stable_id}


@router.post("/stems/push-missing")
def push_missing_stems(body: StemPushMissingIn) -> dict[str, str]:
    data_dir = Path(body.data_dir) if body.data_dir else None
    rc = stems_sync.push_missing(data_dir=data_dir, dry_run=body.dry_run)
    if rc != 0:
        raise HTTPException(status_code=500, detail="push-missing failed")
    return {"status": "ok"}


def _resolve_playlist_stable_ids(data_dir: Path, playlist: str) -> list[str]:
    from apps.vocals.cli import Ctx, best_playlist_rank, load_tracks

    ctx = Ctx(data_dir=data_dir)
    tracks = load_tracks(ctx, playlist)
    rank = best_playlist_rank(ctx, tracks)
    ordered = sorted(tracks, key=lambda t: rank.get(t.stable_id, 1 << 30))
    return [t.stable_id for t in ordered]


@router.post("/stems/bulk-hydrate", response_model=StemBulkHydrateOut)
def bulk_hydrate_stems(body: StemBulkHydrateIn) -> StemBulkHydrateOut:
    """Agent-native parity for ``python -m apps.stems bulk-hydrate`` (ADR-0024).

    Hydrates as many bundles as fit ``budget_bytes``, skipping the
    reservation guard unless ``include_reserved`` is explicitly true.
    """
    from apps.cloud import stem_hydration, stem_index
    from apps.cloud.eviction import HydrationError
    from apps.lyrics.artifacts import asset_clients_for_mode
    from apps.shared.paths import DATA_DIR
    from apps.stems.cli import stems_dir as _stems_dir_for

    data_dir = Path(body.data_dir) if body.data_dir else DATA_DIR
    if body.stable_ids:
        stable_ids = list(body.stable_ids)
    elif body.playlist:
        stable_ids = _resolve_playlist_stable_ids(data_dir, body.playlist)
    else:
        raise HTTPException(status_code=400, detail="pass stable_ids or playlist")

    try:
        s3, cfg = asset_clients_for_mode(writing=True)
    except Exception as exc:  # credentials / policy misconfiguration
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if s3 is None or cfg is None:
        raise HTTPException(
            status_code=400,
            detail="bulk-hydrate needs cloudsync mode 'cloud' with R2 credentials",
        )
    index = stem_index.load_cached_index(data_dir)
    # Parity with CLI ``bulk-hydrate``: refresh from R2 when the local cache
    # is empty or the caller passes ``refresh_index=True`` explicitly.
    if body.refresh_index or not index:
        index = stem_index.refresh_local_cache_from_r2(cfg, s3, data_dir)
    if not index:
        raise HTTPException(
            status_code=400,
            detail=(
                "no stem bundle index published in R2 "
                "(run build-index --publish after the push rail has journaled bundles)"
            ),
        )
    try:
        report = stem_hydration.bulk_hydrate(
            stable_ids,
            data_dir=data_dir,
            cfg=cfg,
            s3=s3,
            index=index,
            byte_budget=body.budget_bytes,
            include_reserved=body.include_reserved,
            stems_dir=_stems_dir_for(data_dir),
        )
    except HydrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StemBulkHydrateOut(
        fetched=[StemBulkHydrateOutcomeOut(**o.__dict__) for o in report.fetched],
        skipped=[StemBulkHydrateOutcomeOut(**o.__dict__) for o in report.skipped],
        bytes_fetched=report.bytes_fetched,
    )


@router.post("/stems/index/build", response_model=StemIndexBuildOut)
def build_stem_index(body: StemIndexBuildIn) -> StemIndexBuildOut:
    """Agent-native parity for ``python -m apps.stems build-index`` (ADR-0024)."""
    from apps.cloud import stem_index
    from apps.shared.paths import DATA_DIR

    data_dir = Path(body.data_dir) if body.data_dir else DATA_DIR
    default_journal = data_dir / "state" / "stem-r2-migration.jsonl"
    journal_path = Path(body.journal_path) if body.journal_path else default_journal
    index = stem_index.build_index_from_journal(journal_path)
    stem_index.save_cached_index(data_dir, index)
    published = False
    if body.publish:
        from apps.cloud.config import CloudConfig
        from apps.cloud.replicate import boto3_s3_client

        cfg = CloudConfig.from_env()
        stem_index.publish_index(cfg, boto3_s3_client(cfg), index)
        published = True
    n_files = sum(len(files) for files in index.values())
    return StemIndexBuildOut(bundles=len(index), files=n_files, published=published)
