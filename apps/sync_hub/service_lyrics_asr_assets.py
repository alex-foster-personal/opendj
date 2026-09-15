"""Hub endpoints that mint presigned ASR lyric transcript reads (ADR-0049).

Mounted under ``/api/v1/sync/lyrics-asr/*``. The hub holds R2 credentials;
spokes receive only short-lived GET URLs and content digests, never bucket keys.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.cloud import asset_store
from apps.cloud.asset_store import AssetStoreError
from apps.cloud.config import CloudConfig, MissingEnvError
from apps.cloud.lyrics_asr_source import (
    LYRICS_ASR_NOT_FOUND,
    LYRICS_ASR_PRESIGN_FAILED,
    lyrics_asr_object_key,
)
from apps.shared.stable_id import is_safe_stable_id_segment
from apps.shared.state import db as state_db
from apps.sync_hub import service_credentials

router = APIRouter(prefix="/lyrics-asr", tags=["sync-lyrics-asr"])
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


class LyricsAsrPresignResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    expires_in_seconds: int = Field(default=_PRESIGN_EXPIRY_S)
    url: str
    content_hash: str
    size_bytes: int


def _hub_r2_clients() -> tuple[CloudConfig, asset_store.AssetS3Client]:
    try:
        cfg = CloudConfig.from_env()
        asset_store.require_credentials(cfg)
    except MissingEnvError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": LYRICS_ASR_PRESIGN_FAILED,
                "message": f"hub has no R2 credentials configured: {exc}",
            },
        ) from exc
    return cfg, asset_store.boto3_asset_client(cfg)


def _validated_stable_id(stable_id: str) -> None:
    if not is_safe_stable_id_segment(stable_id):
        raise HTTPException(
            status_code=400,
            detail={
                "code": LYRICS_ASR_PRESIGN_FAILED,
                "message": (
                    f"stable_id {stable_id!r} must be 1-128 URL-safe identifier "
                    "characters and usable as one path segment"
                ),
            },
        )


@router.get(
    "/{stable_id}",
    response_model=LyricsAsrPresignResponse,
    responses={
        **_auth("lyrics-asr/presign"),
        404: {
            "description": (
                "LYRICS_ASR_NOT_FOUND when the transcript object is absent from R2"
            )
        },
        502: {
            "description": (
                "LYRICS_ASR_PRESIGN_FAILED when presigning or HEAD failed"
            )
        },
    },
)
def get_lyrics_asr_presign(
    request: Request,
    stable_id: str,
    machine_id: str = Query(min_length=1, description="the calling spoke"),
) -> LyricsAsrPresignResponse:
    """Mint a short-lived GET URL for one ASR transcript JSON object."""
    with _hub_conn(request) as conn:
        _require_credential(request, conn, machine_id, "lyrics-asr/presign")
    _validated_stable_id(stable_id)
    object_key = lyrics_asr_object_key(stable_id)
    try:
        cfg, s3 = _hub_r2_clients()
        head = s3.head_object(cfg.audio_bucket, object_key)
        if head is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": LYRICS_ASR_NOT_FOUND,
                    "message": (
                        f"no ASR transcript at {object_key} for stable_id {stable_id!r}"
                    ),
                },
            )
        got = s3.get_object(cfg.audio_bucket, object_key)
        if got is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": LYRICS_ASR_NOT_FOUND,
                    "message": (
                        f"no ASR transcript at {object_key} for stable_id {stable_id!r}"
                    ),
                },
            )
        body, _etag = got
        content_hash = hashlib.sha256(body).hexdigest()
        url = asset_store.presign_object_key(cfg, object_key, _PRESIGN_EXPIRY_S)
    except HTTPException:
        raise
    except AssetStoreError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": LYRICS_ASR_PRESIGN_FAILED, "message": str(exc)},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": LYRICS_ASR_PRESIGN_FAILED,
                "message": f"hub lyrics-asr presign failed: {exc}",
            },
        ) from exc
    _assert_no_r2_secrets_in_payload(url, content_hash)
    return LyricsAsrPresignResponse(
        stable_id=stable_id,
        expires_in_seconds=_PRESIGN_EXPIRY_S,
        url=url,
        content_hash=content_hash,
        size_bytes=head.size,
    )


def _assert_no_r2_secrets_in_payload(url: str, content_hash: str) -> None:
    forbidden = (
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_ACCOUNT_ID",
        "r2_access_key_id",
        "r2_secret_access_key",
    )
    blob = f"{url}|{content_hash}"
    for token in forbidden:
        if token in blob:
            raise HTTPException(
                status_code=500,
                detail={
                    "code": LYRICS_ASR_PRESIGN_FAILED,
                    "message": "presign response carried forbidden credential material",
                },
            )


__all__ = ["LyricsAsrPresignResponse", "router"]
