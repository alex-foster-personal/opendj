"""Hub endpoint that mints presigned PUTs for lyric stem pair registration.

Mounted under ``/api/v1/sync/lyrics-pair/*`` (LYRICS-13411). The hub holds R2
credentials; a spoke sends the content digests and sizes of one pair's bundle
files and gets back, per file, either ``present`` (the hub's HEAD found the
object) or a short-lived PUT URL for the content-addressed key. The spoke calls
again after uploading, and that call is the verification.

A presigned PUT does not bind the body, so "present" is never taken from a HEAD
alone: the hub reads a stored object back and hashes it, the same digest check
every stem read makes (``asset_store.fetch_asset``). An object whose bytes do not
hash to its key is deleted and refused with ``LYRICS_PAIR_DIGEST_MISMATCH``.
The pair is metered like a stem bundle: the same CloudStems gate (as a write)
and the same per-``stable_id`` monthly track charge as ``stems/bundle-presign``.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.cloud import asset_store
from apps.cloud.asset_store import AssetStoreError
from apps.cloud.config import CloudConfig, MissingEnvError
from apps.cloud.hub_lyrics_pair_client import LYRICS_PAIR_DIGEST_MISMATCH, LYRICS_PAIR_PRESIGN_FAILED
from apps.shared.stable_id import is_safe_stable_id_segment
from apps.sync_hub import entitlement_gate, service_credentials
from apps.sync_hub.service_request import (
    _asset_hub_conn as _hub_conn,
)
from apps.sync_hub.service_request import (
    _require_credential,
)
from apps.sync_hub.service_stem_assets import _charge_bundle, _gate_stems, _meter_plan, _meter_refusals

router = APIRouter(prefix="/lyrics-pair", tags=["sync-lyrics-pair"])
_auth = service_credentials.credential_responses

_PRESIGN_EXPIRY_S: int = asset_store.DEFAULT_PRESIGN_EXPIRY_SECONDS
_MAX_FILES = 3
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class PairUploadFileIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    content_hash: str = Field(pattern=_DIGEST_PATTERN)
    size_bytes: int = Field(gt=0)


class PairUploadPresignRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    machine_id: str = Field(min_length=1)
    stable_id: str
    files: list[PairUploadFileIn] = Field(min_length=1, max_length=_MAX_FILES)


class PairUploadFileOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    content_hash: str
    present: bool
    url: str | None = None


class PairUploadPresignResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    expires_in_seconds: int = Field(default=_PRESIGN_EXPIRY_S)
    files: list[PairUploadFileOut]


def _hub_r2_clients() -> tuple[CloudConfig, asset_store.AssetS3Client]:
    """Build the hub's own R2 clients or answer 503 naming the missing config."""
    try:
        cfg = CloudConfig.from_env()
        asset_store.require_credentials(cfg)
    except MissingEnvError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": LYRICS_PAIR_PRESIGN_FAILED,
                "message": f"hub has no R2 credentials configured: {exc}",
            },
        ) from exc
    return cfg, asset_store.boto3_asset_client(cfg)


def _stored_digest_ok(cfg: CloudConfig, s3: asset_store.AssetS3Client, digest: str) -> bool | None:
    """True when the stored object hashes to ``digest``, None when it is gone.

    False means wrong bytes sat under the digest key and were deleted: no
    reader can use them (every read re-checks the digest) and leaving them
    would make the key look present and block the real upload.
    """
    key = asset_store.asset_object_key(digest)
    got = s3.get_object(cfg.audio_bucket, key)
    if got is None:
        return None
    if hashlib.sha256(got[0]).hexdigest() == digest:
        return True
    s3.delete_object(cfg.audio_bucket, key)
    return False


def _targets_for(
    cfg: CloudConfig, s3: asset_store.AssetS3Client, files: list[PairUploadFileIn]
) -> list[PairUploadFileOut]:
    """Present only when the stored bytes hash to the digest, else presign a PUT; 409 on wrong bytes."""
    out: list[PairUploadFileOut] = []
    mismatched: list[str] = []
    for file in files:
        head = s3.head_object(cfg.audio_bucket, asset_store.asset_object_key(file.content_hash))
        verdict = None if head is None else _stored_digest_ok(cfg, s3, file.content_hash)
        if verdict:
            out.append(PairUploadFileOut(content_hash=file.content_hash, present=True))
            continue
        if verdict is False:
            mismatched.append(file.content_hash)
        url = asset_store.presign_url(cfg, file.content_hash, _PRESIGN_EXPIRY_S, method="PUT")
        out.append(PairUploadFileOut(content_hash=file.content_hash, present=False, url=url))
    if mismatched:
        raise HTTPException(
            status_code=409,
            detail={
                "code": LYRICS_PAIR_DIGEST_MISMATCH,
                "message": (
                    f"uploaded bytes do not hash to {', '.join(mismatched)}; the hub deleted "
                    "them, upload the real bytes again"
                ),
            },
        )
    return out


@router.post(
    "/upload-presign",
    response_model=PairUploadPresignResponse,
    responses=entitlement_gate.merge_responses(
        _auth("lyrics-pair/upload-presign"),
        entitlement_gate.refusals("lyrics-pair/upload-presign"),
        {
            400: {"description": "LYRICS_PAIR_PRESIGN_FAILED when stable_id is not one safe path segment"},
            409: {
                "description": "LYRICS_PAIR_DIGEST_MISMATCH when a stored object does not hash to its digest",
                "model": entitlement_gate.GateErrorResponse,
            },
            502: {"description": "LYRICS_PAIR_PRESIGN_FAILED when presigning or HEAD failed"},
            503: {"description": "LYRICS_PAIR_PRESIGN_FAILED when the hub has no R2 credentials"},
        },
        _meter_refusals(),
    ),
)
def post_lyrics_pair_upload_presign(request: Request, payload: PairUploadPresignRequest) -> PairUploadPresignResponse:
    """Mint short-lived PUT URLs for the absent files of one lyric stem pair bundle."""
    with _hub_conn(request) as conn:
        _require_credential(request, conn, payload.machine_id, "lyrics-pair/upload-presign")
        if not is_safe_stable_id_segment(payload.stable_id):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": LYRICS_PAIR_PRESIGN_FAILED,
                    "message": f"stable_id {payload.stable_id!r} must be usable as one path segment",
                },
            )
        owner_sub = _gate_stems(request, conn, payload.machine_id, "write")
        meter_plan = _meter_plan(conn, owner_sub=owner_sub, stable_id=payload.stable_id)
    try:
        cfg, s3 = _hub_r2_clients()
        files_out = _targets_for(cfg, s3, payload.files)
    except HTTPException:
        raise
    except AssetStoreError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": LYRICS_PAIR_PRESIGN_FAILED, "message": str(exc)},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": LYRICS_PAIR_PRESIGN_FAILED,
                "message": f"hub lyrics-pair presign failed: {exc}",
            },
        ) from exc
    _assert_no_r2_secrets_in_payload(files_out, cfg)
    # Charged last, as stems/bundle-presign does: a refused or failed request
    # costs no track, and the confirm call re-charges the same row for free.
    if meter_plan is not None:
        owner, _precheck_month, cap = meter_plan
        _charge_bundle(request, owner_sub=owner, cap=cap, stable_id=payload.stable_id)
    return PairUploadPresignResponse(stable_id=payload.stable_id, expires_in_seconds=_PRESIGN_EXPIRY_S, files=files_out)


def _assert_no_r2_secrets_in_payload(files: list[PairUploadFileOut], cfg: CloudConfig) -> None:
    """Fail closed if a presign response ever carried the hub's secret key."""
    blob = "|".join(f"{f.content_hash}|{f.url or ''}" for f in files)
    if cfg.r2_secret_access_key and cfg.r2_secret_access_key in blob:
        raise HTTPException(
            status_code=500,
            detail={
                "code": LYRICS_PAIR_PRESIGN_FAILED,
                "message": "presign response carried forbidden credential material",
            },
        )


__all__ = ["PairUploadPresignRequest", "PairUploadPresignResponse", "router"]
