"""Hub endpoints that mint presigned stem bundle reads (ADR-0051).

Mounted under ``/api/v1/sync/stems/*``. The hub holds R2 credentials; spokes
receive only short-lived GET URLs and content digests, never bucket keys.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.cloud import asset_store, stem_index
from apps.cloud.asset_store import AssetStoreError
from apps.cloud.config import CloudConfig, MissingEnvError
from apps.cloud.stem_source import STEM_BUNDLE_NOT_INDEXED, STEM_BUNDLE_PRESIGN_FAILED
from apps.shared.stable_id import is_safe_stable_id_segment
from apps.shared.state import db as state_db
from apps.sync_hub import service_credentials

StemIndexFetcher = Callable[[], stem_index.StemAssetIndex]

router = APIRouter(prefix="/stems", tags=["sync-stems"])
_auth = service_credentials.credential_responses

_PRESIGN_EXPIRY_S: int = asset_store.DEFAULT_PRESIGN_EXPIRY_SECONDS


def _db_path(request: Request) -> Path:
    configured = getattr(request.app.state, "state_db_path", None)
    if configured is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "SYNC_NO_DB",
                "message": "app.state.state_db_path is unset",
            },
        )
    return Path(str(configured))


def _data_dir(request: Request) -> Path:
    configured = getattr(request.app.state, "sync_hub_data_dir", None)
    if configured is not None:
        return Path(str(configured))
    return _db_path(request).resolve().parent.parent


@contextmanager
def _hub_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = state_db.open_rw(_db_path(request))
    try:
        yield conn
    finally:
        conn.close()


def _require_credential(
    request: Request, conn: sqlite3.Connection, machine_id: str, endpoint: str
) -> service_credentials.CredentialVerdict:
    return service_credentials.require_credential(
        request, conn, machine_id, data_dir=_data_dir(request), endpoint=endpoint
    )


class StemIndexResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: dict[str, dict[str, str]]


class StemPresignFileOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    filename: str
    content_hash: str
    size_bytes: int
    url: str


class StemBundlePresignResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    expires_in_seconds: int = Field(default=_PRESIGN_EXPIRY_S)
    files: tuple[StemPresignFileOut, ...]


def _hub_r2_clients() -> tuple[CloudConfig, asset_store.AssetS3Client]:
    try:
        cfg = CloudConfig.from_env()
        asset_store.require_credentials(cfg)
    except MissingEnvError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": STEM_BUNDLE_PRESIGN_FAILED,
                "message": f"hub has no R2 credentials configured: {exc}",
            },
        ) from exc
    return cfg, asset_store.boto3_asset_client(cfg)


def _validated_stable_id(stable_id: str) -> None:
    if not is_safe_stable_id_segment(stable_id):
        raise HTTPException(
            status_code=400,
            detail={
                "code": STEM_BUNDLE_PRESIGN_FAILED,
                "message": (
                    f"stable_id {stable_id!r} must be 1-128 URL-safe identifier "
                    "characters and usable as one path segment"
                ),
            },
        )


def _validated_index_entry(
    stable_id: str, index: stem_index.StemAssetIndex
) -> dict[str, str]:
    _validated_stable_id(stable_id)
    file_hashes = index.get(stable_id)
    if not file_hashes:
        raise HTTPException(
            status_code=404,
            detail={
                "code": STEM_BUNDLE_NOT_INDEXED,
                "message": f"stable_id {stable_id!r} is not in the published stem index",
            },
        )
    if stem_index.MANIFEST_FILENAME not in file_hashes:
        raise HTTPException(
            status_code=502,
            detail={
                "code": STEM_BUNDLE_PRESIGN_FAILED,
                "message": "index entry has no manifest.json hash",
            },
        )
    bad = sorted(
        name
        for name in file_hashes
        if not stem_index.is_allowed_stem_filename(name)
    )
    if bad:
        raise HTTPException(
            status_code=502,
            detail={
                "code": STEM_BUNDLE_PRESIGN_FAILED,
                "message": f"index has disallowed filename(s): {', '.join(bad)}",
            },
        )
    validated: dict[str, str] = {}
    for filename, digest in file_hashes.items():
        try:
            validated[filename] = asset_store.validate_content_hash(digest)
        except AssetStoreError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": STEM_BUNDLE_PRESIGN_FAILED,
                    "message": (
                        f"index entry for {stable_id!r} has invalid digest for "
                        f"{filename!r}: {exc}"
                    ),
                },
            ) from exc
    return validated


@router.get(
    "/index",
    response_model=StemIndexResponse,
    responses=_auth("stems/index"),
)
def get_stem_index(
    request: Request,
    machine_id: str = Query(min_length=1, description="the calling spoke"),
) -> StemIndexResponse:
    """Return the published stem bundle index for an authenticated spoke.

    ``request.app.state.stem_index_fetcher``, when set by the code that built
    the app, replaces the real R2 index fetch with the callable's return
    value. Production never sets it; only test app-builders do. The
    credential check always runs first regardless.
    """
    with _hub_conn(request) as conn:
        _require_credential(request, conn, machine_id, "stems/index")
    fetcher: StemIndexFetcher | None = getattr(request.app.state, "stem_index_fetcher", None)
    try:
        if fetcher is not None:
            index = fetcher()
        else:
            cfg, s3 = _hub_r2_clients()
            index = stem_index.fetch_index(cfg, s3)
    except HTTPException:
        raise
    except stem_index.StemIndexError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": STEM_BUNDLE_PRESIGN_FAILED, "message": str(exc)},
        ) from exc
    except AssetStoreError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": STEM_BUNDLE_PRESIGN_FAILED, "message": str(exc)},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": STEM_BUNDLE_PRESIGN_FAILED,
                "message": f"hub stem index fetch failed: {exc}",
            },
        ) from exc
    return StemIndexResponse(index=index)


@router.get(
    "/bundle-presign",
    response_model=StemBundlePresignResponse,
    responses={
        **_auth("stems/bundle-presign"),
        404: {
            "description": (
                "STEM_BUNDLE_NOT_INDEXED when the stable_id is absent from the "
                "published index"
            )
        },
        502: {
            "description": (
                "STEM_BUNDLE_PRESIGN_FAILED when presigning or HEAD failed"
            )
        },
    },
)
def get_bundle_presign(
    request: Request,
    machine_id: str = Query(min_length=1, description="the calling spoke"),
    stable_id: str = Query(min_length=1, description="track stable_id"),
) -> StemBundlePresignResponse:
    """Mint short-lived GET URLs for every file in one indexed bundle."""
    with _hub_conn(request) as conn:
        _require_credential(request, conn, machine_id, "stems/bundle-presign")
    try:
        cfg, s3 = _hub_r2_clients()
        index = stem_index.fetch_index(cfg, s3)
    except HTTPException:
        raise
    except stem_index.StemIndexError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": STEM_BUNDLE_PRESIGN_FAILED, "message": str(exc)},
        ) from exc
    except AssetStoreError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": STEM_BUNDLE_PRESIGN_FAILED, "message": str(exc)},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": STEM_BUNDLE_PRESIGN_FAILED,
                "message": f"hub stem index fetch failed: {exc}",
            },
        ) from exc
    file_hashes = _validated_index_entry(stable_id, index)
    files_out: list[StemPresignFileOut] = []
    for filename, digest in sorted(file_hashes.items()):
        try:
            head = asset_store.head_asset(cfg, s3, digest)
            if head is None:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": STEM_BUNDLE_PRESIGN_FAILED,
                        "message": f"indexed object missing for {filename} ({digest})",
                    },
                )
            url = asset_store.presign_url(cfg, digest, _PRESIGN_EXPIRY_S)
        except asset_store.AssetStoreError as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": STEM_BUNDLE_PRESIGN_FAILED, "message": str(exc)},
            ) from exc
        files_out.append(
            StemPresignFileOut(
                filename=filename,
                content_hash=digest,
                size_bytes=head.size,
                url=url,
            )
        )
    _assert_no_r2_secrets_in_payload(files_out)
    return StemBundlePresignResponse(
        stable_id=stable_id,
        expires_in_seconds=_PRESIGN_EXPIRY_S,
        files=tuple(files_out),
    )


def _assert_no_r2_secrets_in_payload(files: list[StemPresignFileOut]) -> None:
    """Fail closed if a presign response ever carried bucket credentials."""
    forbidden = (
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_ACCOUNT_ID",
        "r2_access_key_id",
        "r2_secret_access_key",
    )
    for entry in files:
        blob = f"{entry.url}|{entry.content_hash}|{entry.filename}"
        for token in forbidden:
            if token in blob:
                raise HTTPException(
                    status_code=500,
                    detail={
                        "code": STEM_BUNDLE_PRESIGN_FAILED,
                        "message": "presign response carried forbidden credential material",
                    },
                )


__all__ = [
    "StemBundlePresignResponse",
    "StemIndexResponse",
    "StemPresignFileOut",
    "router",
]
