"""Hub endpoints that mint presigned stem bundle reads (ADR-0051).

Mounted under ``/api/v1/sync/stems/*``. The hub holds R2 credentials; spokes
receive only short-lived GET URLs and content digests, never bucket keys.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.cloud import asset_store, stem_index
from apps.cloud.asset_store import AssetStoreError
from apps.cloud.config import CloudConfig, MissingEnvError
from apps.cloud.stem_source import STEM_BUNDLE_NOT_INDEXED, STEM_BUNDLE_PRESIGN_FAILED
from apps.shared.stable_id import is_safe_stable_id_segment
from apps.shared.state import db as state_db
from apps.sync_hub import entitlement_gate, service_credentials, stem_meter
from apps.sync_hub.service_request import (
    SYNC_NO_DB_MESSAGE_ASSET_ROUTERS,
    _data_dir,
    _db_path,
)

StemIndexFetcher = Callable[[], stem_index.StemAssetIndex]

router = APIRouter(prefix="/stems", tags=["sync-stems"])
_auth = service_credentials.credential_responses

_PRESIGN_EXPIRY_S: int = asset_store.DEFAULT_PRESIGN_EXPIRY_SECONDS


@contextmanager
def _hub_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = state_db.open_rw(
        _db_path(request, sync_no_db_message=SYNC_NO_DB_MESSAGE_ASSET_ROUTERS)
    )
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


def _gate_stems(
    request: Request, conn: sqlite3.Connection, machine_id: str, op: entitlement_gate.Operation
) -> str | None:
    """Hosted-hub CloudStems entitlement; the entitled owner, None when self-hosted."""
    return entitlement_gate.require(
        request,
        conn,
        machine_id=machine_id,
        operation=op,
        data_dir=lambda: _data_dir(request),
        feature_id=entitlement_gate.CLOUDSTEMS_HOSTED_FEATURE,
    )


def _cap_exceeded(exc: stem_meter.StemCapExceeded) -> HTTPException:
    """The 429 a spent month answers with, in one place for both checks."""
    return HTTPException(
        status_code=429,
        detail={
            "code": stem_meter.CAP_EXCEEDED_CODE,
            "message": (
                f"{exc}. CloudStems is sold at {exc.cap} tracks a month and "
                f"this owner has used all of {exc.month}'s allowance; it "
                "resets at the start of the next UTC month. Bundles "
                "already taken this month keep serving."
            ),
        },
    )


def _meter_plan(
    conn: sqlite3.Connection, *, owner_sub: str | None, stable_id: str
) -> tuple[str, str, int] | None:
    """Decide who to meter and refuse an obviously spent month up front.

    ``owner_sub`` is what :func:`_gate_stems` returned: the owner whose
    entitlement passed, or None on a self-hosted hub. Returns ``(owner_sub, month, cap)`` on a hosted hub, ``None`` when the hub
    is self-hosted (the operator is the user, nothing is metered). This runs
    only after the credential and entitlement checks, so a refusal by either
    never consumes a track.

    It does NOT charge: :func:`_charge_bundle` does that after the presign
    actually succeeded, so an unindexed ``stable_id`` or an R2 failure does
    not use up an owner's month. The cap has no default: a hosted hub that
    meters CloudStems but was never told the number answers 503 rather than
    inventing one.
    """
    if owner_sub is None:
        return None
    try:
        cap = stem_meter.parse_cap(os.environ)
    except stem_meter.StemCapConfigError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": stem_meter.CAP_UNSET_CODE, "message": str(exc)},
        ) from exc
    month = stem_meter.month_of(datetime.now(UTC))
    try:
        stem_meter.check_room(
            conn, owner_sub=owner_sub, month=month, stable_id=stable_id, cap=cap
        )
    except stem_meter.StemCapExceeded as exc:
        raise _cap_exceeded(exc) from exc
    return owner_sub, month, cap


def _meter_refusals() -> dict[int | str, dict[str, object]]:
    """The two refusals the monthly meter adds to this route (CORE review, P2).

    Declared next to the code that raises them so the contract cannot drift
    from the behaviour: a client generated from ``openapi.json`` must be able
    to type and present an exhausted month rather than treat 429 as impossible.
    """
    return {
        429: {
            # The BODY model matters as much as the status: a description-only
            # entry regenerates into api-types.ts as a response with no
            # content, so a typed client cannot read detail.code at all
            # (Codex round 8). Both refusals answer {"detail": {code, message}}.
            "model": entitlement_gate.GateErrorResponse,
            "description": (
                f"{stem_meter.CAP_EXCEEDED_CODE} when this owner has used every "
                f"CloudStems track in the current UTC month; the "
                f"{stem_meter.CAP_ENV} allowance resets at the start of the next "
                "month, and bundles already taken this month keep serving"
            )
        },
        503: {
            "model": entitlement_gate.GateErrorResponse,
            "description": (
                f"{stem_meter.CAP_UNSET_CODE} when this hub meters CloudStems but "
                f"{stem_meter.CAP_ENV} is unset, non-ASCII, or not a whole number "
                ">= 1"
            )
        },
    }


def _charge_bundle(
    request: Request, *, owner_sub: str, cap: int, stable_id: str
) -> None:
    """Count one bundle the hub has ALREADY presigned (CORE review, P1).

    ``stem_meter.charge`` opens the ``BEGIN IMMEDIATE`` transaction: production
    connections autocommit, so the insert and the count have to be serialized
    or two requests can both take the last track.

    The month is derived HERE, from the same instant as ``counted_at``, not
    taken from the pre-check: a request that starts before 00:00 UTC and
    finishes its index/HEAD/presign work after it would otherwise charge
    October's allowance with a November timestamp (Codex round 6, non-blocking).
    """
    now = datetime.now(UTC)
    month = stem_meter.month_of(now)
    with _hub_conn(request) as conn:
        try:
            stem_meter.charge(
                conn,
                owner_sub=owner_sub,
                month=month,
                stable_id=stable_id,
                cap=cap,
                counted_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        except stem_meter.StemCapExceeded as exc:
            # Another request took the last track between the pre-check and
            # this charge. The presign already happened, but this 429 carries
            # no URLs, so the client cannot download the bundle: the request
            # is refused and nothing was counted.
            raise _cap_exceeded(exc) from exc


def _head_every_file(
    cfg: CloudConfig, s3: asset_store.AssetS3Client, file_hashes: dict[str, str]
) -> list[StemSizeFileOut]:
    """HEAD every indexed file of one bundle, minting no URL and charging nothing.

    This is what a size probe reads. HEAD is the only R2 call: no
    ``presign_url`` is minted, so no capability to read the bytes exists, and
    the route that serves it never calls :func:`_charge_bundle` (Codex round
    3, P1). A caller that only wants to know whether a bundle fits a byte
    budget therefore costs the owner nothing.
    """
    files_out: list[StemSizeFileOut] = []
    for filename, digest in sorted(file_hashes.items()):
        try:
            head = asset_store.head_asset(cfg, s3, digest)
        except asset_store.AssetStoreError as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": STEM_BUNDLE_PRESIGN_FAILED, "message": str(exc)},
            ) from exc
        if head is None:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": STEM_BUNDLE_PRESIGN_FAILED,
                    "message": f"indexed object missing for {filename} ({digest})",
                },
            )
        files_out.append(
            StemSizeFileOut(
                filename=filename,
                content_hash=digest,
                size_bytes=head.size,
            )
        )
    return files_out


def _presign_every_file(
    cfg: CloudConfig, s3: asset_store.AssetS3Client, file_hashes: dict[str, str]
) -> list[StemPresignFileOut]:
    """Head and presign every indexed file of one bundle.

    Split out of the route so the route reads as credential -> gate ->
    pre-check -> presign -> charge, and so the CC budget of
    :func:`get_bundle_presign` stays where it was.
    """
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
    return files_out


class StemIndexResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: dict[str, dict[str, str]]


class StemPresignFileOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    filename: str
    content_hash: str
    size_bytes: int
    url: str


class StemSizeFileOut(BaseModel):
    """One indexed file, measured but not made readable: there is no ``url``."""

    model_config = ConfigDict(frozen=True)

    filename: str
    content_hash: str
    size_bytes: int


class StemBundleSizeResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    files: tuple[StemSizeFileOut, ...]


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
    responses=entitlement_gate.merge_responses(_auth("stems/index"), entitlement_gate.refusals("stems/index")),
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
        _gate_stems(request, conn, machine_id, "read")
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
    responses=entitlement_gate.merge_responses(
        _auth("stems/bundle-presign"),
        entitlement_gate.refusals("stems/bundle-presign"),
        {
            400: {
                "description": (
                    "STEM_BUNDLE_PRESIGN_FAILED when the stable_id is not a safe "
                    "bundle id"
                ),
                "model": entitlement_gate.GateErrorResponse,
            },
            404: {
                "description": (
                    "STEM_BUNDLE_NOT_INDEXED when the stable_id is absent from the "
                    "published index"
                ),
                "model": entitlement_gate.GateErrorResponse,
            },
            502: {
                "description": (
                    "STEM_BUNDLE_PRESIGN_FAILED when presigning or HEAD failed"
                ),
                "model": entitlement_gate.GateErrorResponse,
            },
        },
        _meter_refusals(),
    ),
)
def get_bundle_presign(
    request: Request,
    machine_id: str = Query(min_length=1, description="the calling spoke"),
    stable_id: str = Query(min_length=1, description="track stable_id"),
) -> StemBundlePresignResponse:
    """Mint short-lived GET URLs for every file in one indexed bundle."""
    with _hub_conn(request) as conn:
        _require_credential(request, conn, machine_id, "stems/bundle-presign")
        # Bill the owner whose entitlement passed, never a second enrollment
        # read that a concurrent reassignment could change (Sol, P1).
        owner_sub = _gate_stems(request, conn, machine_id, "read")
        meter_plan = _meter_plan(conn, owner_sub=owner_sub, stable_id=stable_id)
    cfg, s3, file_hashes = _indexed_bundle(machine_id, stable_id)
    files_out = _presign_every_file(cfg, s3, file_hashes)
    _assert_no_r2_secrets_in_payload(files_out)
    # Charged LAST: the bundle exists and every object was headed and
    # presigned, so only now does this request cost the owner a track
    # (CORE review, P1). An unindexed stable_id or an R2 failure above
    # answers 404/502 with the owner's month untouched.
    if meter_plan is not None:
        # The pre-check's month is only a hint: a request that crossed the UTC
        # boundary during the presign must charge the month it lands in.
        owner_sub, _precheck_month, cap = meter_plan
        _charge_bundle(request, owner_sub=owner_sub, cap=cap, stable_id=stable_id)
    return StemBundlePresignResponse(
        stable_id=stable_id,
        expires_in_seconds=_PRESIGN_EXPIRY_S,
        files=tuple(files_out),
    )


def _indexed_bundle(
    machine_id: str, stable_id: str
) -> tuple[CloudConfig, asset_store.AssetS3Client, dict[str, str]]:
    """R2 clients plus the validated index entry for one bundle.

    Shared by the charged presign route and the unmetered size probe so both
    answer the same 404/502 for an unindexed id, a missing object set or a
    disallowed filename. ``machine_id`` is unused beyond the callers' gating
    today; it stays in the signature so a future per-machine index cannot
    silently serve the wrong entry.
    """
    del machine_id
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
    return cfg, s3, _validated_index_entry(stable_id, index)


@router.get(
    "/bundle-size",
    response_model=StemBundleSizeResponse,
    responses=entitlement_gate.merge_responses(
        _auth("stems/bundle-size"),
        entitlement_gate.refusals("stems/bundle-size"),
        {
            400: {
                "description": (
                    "STEM_BUNDLE_PRESIGN_FAILED when the stable_id is not a safe "
                    "bundle id"
                ),
                "model": entitlement_gate.GateErrorResponse,
            },
            404: {
                "description": (
                    "STEM_BUNDLE_NOT_INDEXED when the stable_id is absent from the "
                    "published index"
                ),
                "model": entitlement_gate.GateErrorResponse,
            },
            502: {
                "description": (
                    "STEM_BUNDLE_PRESIGN_FAILED when the index or a HEAD failed"
                ),
                "model": entitlement_gate.GateErrorResponse,
            },
        },
    ),
)
def get_bundle_size(
    request: Request,
    machine_id: str = Query(min_length=1, description="the calling spoke"),
    stable_id: str = Query(min_length=1, description="track stable_id"),
) -> StemBundleSizeResponse:
    """Measure one indexed bundle without minting URLs or spending a track.

    Same credential and entitlement gates as ``bundle-presign`` and the same
    index entry, but this route does NOT call ``_meter_plan`` and never
    charges: it is unmetered even when the owner's month is spent, and it
    answers no ``url`` for any file, so nothing it returns can be downloaded.
    ``HubPresignedSource.bundle_remote_size`` uses it before ``bulk_hydrate``
    decides whether a bundle fits ``byte_budget`` (Codex round 3, P1).
    """
    with _hub_conn(request) as conn:
        _require_credential(request, conn, machine_id, "stems/bundle-size")
        _gate_stems(request, conn, machine_id, "read")
    cfg, s3, file_hashes = _indexed_bundle(machine_id, stable_id)
    return StemBundleSizeResponse(
        stable_id=stable_id,
        files=tuple(_head_every_file(cfg, s3, file_hashes)),
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
