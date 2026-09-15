"""CloudSync stem bundle push/hydrate routes (agent-native parity, D13.3).

Sibling of ``routes/stems.py``, which serves validated local bundles under
``/tracks/{stable_id}/stems``. These endpoints drive the migration rail and
the hash-based hydrate path without going through the track-scoped loader.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NoReturn

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from apps.cloud.asset_store import AssetStoreError
from apps.cloud.eviction import HydrationError
from apps.lyrics import stems_sync
from apps.stems.external_roots import EXTERNAL_ROOTS_ENV
from apps.vocals.errors import UnknownPlaylistError
from apps.webui.server.routes.stems_parity_guard import (
    guard_stems_parity_call,
    unknown_playlist_response,
)

router = APIRouter(tags=["stems"])

STEM_HYDRATE_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "description": (
            "Invalid or missing manifest_path, corrupt manifest, or hydrate refused"
        ),
    },
}

STEM_PUSH_MISSING_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "MDT_EXTERNAL_STEM_ROOTS entry does not exist"},
    503: {"description": "MDT_EXTERNAL_STEM_ROOTS not configured"},
}

STEM_BULK_HYDRATE_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "description": (
            "Invalid request, missing R2 credentials, or no published stem "
            "bundle index in R2"
        )
    },
    404: {"description": "Unknown playlist name (detail + known sibling keys)"},
    502: {
        "description": (
            "Local stem bundle index cache is present but unreadable "
            "(STEM_INDEX_CORRUPT) or other hub index/presign failure"
        )
    },
    503: {
        "description": (
            "Configured sync hub unreachable (SYNC_HUB_UNREACHABLE with endpoint "
            "and underlying error)"
        )
    },
}


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


def _structured_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _raise_hydrate_error(exc: BaseException) -> NoReturn:
    if isinstance(exc, FileNotFoundError):
        raise _structured_error(
            400,
            "STEM_MANIFEST_PATH_NOT_FOUND",
            f"manifest_path does not exist: {exc.filename or exc}",
        ) from exc
    if isinstance(exc, HydrationError):
        raise _structured_error(400, "STEM_HYDRATE_REFUSED", str(exc)) from exc
    if isinstance(exc, ValueError):
        raise _structured_error(400, "STEM_MANIFEST_INVALID", str(exc)) from exc
    if isinstance(exc, json.JSONDecodeError):
        raise _structured_error(400, "STEM_MANIFEST_INVALID", str(exc)) from exc
    if isinstance(exc, AssetStoreError):
        raise _structured_error(400, "STEM_HYDRATE_FETCH_FAILED", str(exc)) from exc
    raise exc


def _raise_stem_source_error(exc: BaseException, *, endpoint: str) -> NoReturn:
    from apps.cloud.stem_source import StemSourceError, hub_transport_failure_kind
    from apps.webui.server.routes.sync_hub_route_errors import raise_sync_hub_unreachable

    if not isinstance(exc, StemSourceError):
        raise exc
    if hub_transport_failure_kind(exc) == "unreachable":
        raise_sync_hub_unreachable(endpoint, exc)
    raise HTTPException(
        status_code=502,
        detail={"code": exc.code, "message": exc.message},
    ) from exc


def _raise_push_missing_error(exc: BaseException) -> NoReturn:
    if isinstance(exc, RuntimeError) and EXTERNAL_ROOTS_ENV in str(exc):
        raise _structured_error(
            503,
            "MDT_EXTERNAL_STEM_ROOTS_MISSING",
            str(exc),
        ) from exc
    if isinstance(exc, FileNotFoundError) and EXTERNAL_ROOTS_ENV in str(exc):
        raise _structured_error(
            400,
            "MDT_EXTERNAL_STEM_ROOTS_INVALID",
            str(exc),
        ) from exc
    raise exc


@router.post(
    "/stems/{stable_id}/hydrate",
    responses=STEM_HYDRATE_RESPONSES,
)
def hydrate_stem(stable_id: str, body: StemHydrateIn) -> dict[str, str]:
    def _run() -> dict[str, str]:
        manifest_path = Path(body.manifest_path)
        if not manifest_path.is_file():
            raise _structured_error(
                400,
                "STEM_MANIFEST_PATH_NOT_FOUND",
                f"manifest_path does not exist: {body.manifest_path}",
            )
        data_dir = Path(body.data_dir) if body.data_dir else None
        try:
            rc = stems_sync.hydrate(
                stable_id,
                manifest_path=manifest_path,
                data_dir=data_dir,
                dry_run=body.dry_run,
            )
        except (
            FileNotFoundError,
            HydrationError,
            ValueError,
            json.JSONDecodeError,
            AssetStoreError,
        ) as exc:
            _raise_hydrate_error(exc)
        if rc != 0:
            raise _structured_error(500, "STEM_HYDRATE_FAILED", "hydrate failed")
        return {"status": "ok", "stable_id": stable_id}

    return guard_stems_parity_call(_run)


@router.post(
    "/stems/push-missing",
    responses=STEM_PUSH_MISSING_RESPONSES,
)
def push_missing_stems(body: StemPushMissingIn) -> dict[str, str]:
    def _run() -> dict[str, str]:
        data_dir = Path(body.data_dir) if body.data_dir else None
        try:
            rc = stems_sync.push_missing(data_dir=data_dir, dry_run=body.dry_run)
        except (RuntimeError, FileNotFoundError) as exc:
            _raise_push_missing_error(exc)
        if rc != 0:
            raise _structured_error(500, "STEM_PUSH_MISSING_FAILED", "push-missing failed")
        return {"status": "ok"}

    return guard_stems_parity_call(_run)


def _resolve_playlist_stable_ids(data_dir: Path, playlist: str) -> list[str]:
    from apps.vocals.cli import Ctx, best_playlist_rank, load_tracks

    ctx = Ctx(data_dir=data_dir)
    tracks = load_tracks(ctx, playlist)
    rank = best_playlist_rank(ctx, tracks)
    ordered = sorted(tracks, key=lambda t: rank.get(t.stable_id, 1 << 30))
    return [t.stable_id for t in ordered]


@router.post(
    "/stems/bulk-hydrate",
    response_model=StemBulkHydrateOut,
    responses=STEM_BULK_HYDRATE_RESPONSES,
)
def bulk_hydrate_stems(body: StemBulkHydrateIn) -> StemBulkHydrateOut | JSONResponse:
    """Agent-native parity for ``python -m apps.stems bulk-hydrate`` (ADR-0024).

    Hydrates as many bundles as fit ``budget_bytes``, skipping the
    reservation guard unless ``include_reserved`` is explicitly true.
    """
    try:
        return guard_stems_parity_call(lambda: _bulk_hydrate_stems_impl(body))
    except UnknownPlaylistError as exc:
        return unknown_playlist_response(exc)


def _bulk_hydrate_stems_impl(body: StemBulkHydrateIn) -> StemBulkHydrateOut:
    from apps.cloud import stem_hydration, stem_index
    from apps.cloud.eviction import HydrationError
    from apps.cloud.hub_stem_client import STEM_BUNDLE_PRESIGN_PATH, STEM_INDEX_PATH
    from apps.cloud.stem_source import StemSourceError, resolve_stem_hydration_source
    from apps.shared.paths import DATA_DIR
    from apps.stems.cli import stems_dir as _stems_dir_for

    data_dir = Path(body.data_dir) if body.data_dir else DATA_DIR
    if body.stable_ids:
        stable_ids = list(body.stable_ids)
    elif body.playlist:
        stable_ids = _resolve_playlist_stable_ids(data_dir, body.playlist)
    else:
        raise HTTPException(status_code=400, detail="pass stable_ids or playlist")

    source = resolve_stem_hydration_source(data_dir)
    if source is None:
        raise HTTPException(
            status_code=400,
            detail="bulk-hydrate needs cloud mode with R2 credentials or a configured hub",
        )
    try:
        index = stem_index.load_cached_index(data_dir)
    except stem_index.StemIndexError as exc:
        from apps.webui.server.routes.stems import _raise_stem_index_corrupt

        _raise_stem_index_corrupt(exc, data_dir)
    # Parity with CLI ``bulk-hydrate``: refresh from R2 when the local cache
    # is empty or the caller passes ``refresh_index=True`` explicitly.
    if body.refresh_index or not index:
        try:
            source.refresh_index(data_dir, force=True)
        except StemSourceError as exc:
            _raise_stem_source_error(exc, endpoint=STEM_INDEX_PATH)
        index = stem_index.load_cached_index(data_dir)
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
            source=source,
            index=index,
            byte_budget=body.budget_bytes,
            include_reserved=body.include_reserved,
            stems_dir=_stems_dir_for(data_dir),
        )
    except StemSourceError as exc:
        _raise_stem_source_error(exc, endpoint=STEM_BUNDLE_PRESIGN_PATH)
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
    return guard_stems_parity_call(lambda: _build_stem_index_impl(body))


def _build_stem_index_impl(body: StemIndexBuildIn) -> StemIndexBuildOut:
    from apps.cloud import stem_index
    from apps.shared.paths import DATA_DIR
    from apps.webui.server.routes.stems import _raise_stem_index_corrupt

    data_dir = Path(body.data_dir) if body.data_dir else DATA_DIR
    default_journal = data_dir / "state" / "stem-r2-migration.jsonl"
    journal_path = Path(body.journal_path) if body.journal_path else default_journal
    try:
        index = stem_index.build_index_from_journal(journal_path)
    except stem_index.StemIndexError as exc:
        _raise_stem_index_corrupt(exc, data_dir)
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
