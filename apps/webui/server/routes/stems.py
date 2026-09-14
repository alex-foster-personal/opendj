"""Read-only endpoints for validated precomputed Demucs stem bundles.

The application integrator mounts :data:`router` at ``/api/v1``.  The router
does not run Demucs or mutate files: every request reloads and validates the
stored artifact before it exposes either the manifest or a WAV response.
The manifest GET answers HTTP 200 unavailable when no bundle exists; the
part GET still 404s when there is no file to stream.
"""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from io import BufferedReader
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from apps.cloud import stem_hydration, stem_index
from apps.cloud.asset_store import AssetS3Client
from apps.cloud.config import CloudConfig

from ..stem_artifacts import (
    DEFAULT_STEMS_DIR,
    StemArtifactError,
    StemBundle,
    StemBundleNotFoundError,
    load_stem_bundle,
)

router = APIRouter(prefix="/tracks", tags=["stems"])

#: How long the PART route waits for an in-flight hydration before answering
#: "still fetching" instead of the bytes. The MANIFEST route never waits at
#: all (D2): it only enqueues and returns, so a deck load's probe is never
#: held for a 4-part R2 fetch. A part request means the caller already
#: decided it wants stem bytes for THIS track, so waiting here (bounded) is
#: the right tradeoff -- see PR body "which request waits on what".
STEM_PART_HYDRATE_WAIT_S: float = 30.0

_HYDRATE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="stem-hydrate")
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: dict[str, Future] = {}
#: stable_id -> reason, for a bundle the index says exists but whose last
#: hydration attempt failed. Consulted BEFORE re-enqueueing so a hot loop of
#: part requests does not hammer R2 with the same doomed fetch, and so a
#: request can fail loud immediately instead of waiting out the full timeout
#: again for an error already known.
_LAST_HYDRATE_ERROR: dict[str, str] = {}


class StemPartOut(BaseModel):
    """A validated standard part, available from the paired file endpoint."""

    model_config = ConfigDict(frozen=True)

    media_type: str


class StemManifestOut(BaseModel):
    """Frontend manifest projected from a stricter durable artifact manifest."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    schema_version: int = Field(default=1, alias="schema")
    stable_id: str
    source: str = "demucs"
    model: str
    layout: str = "demucs4"
    sample_rate_hz: int
    frame_count: int
    channel_count: int
    parts: dict[str, StemPartOut]


class StemUnavailableOut(BaseModel):
    """HTTP 200 empty-state: no stored bundle for this stable_id."""

    model_config = ConfigDict(frozen=True)

    status: Literal["unavailable"] = "unavailable"
    code: Literal["STEM_BUNDLE_NOT_FOUND", "STEM_BUNDLE_HYDRATING"] = "STEM_BUNDLE_NOT_FOUND"
    stable_id: str
    message: str
    #: True while a background R2 hydration for this bundle is in flight.
    #: Distinct from a genuinely absent bundle (``hydrating=False``): a
    #: client that cares can poll again shortly rather than treating this
    #: the same as "no stems exist for this track".
    hydrating: bool = False


def _stems_dir(request: Request) -> Path:
    """Use an injected directory in isolated apps, else the canonical state dir."""
    configured = getattr(request.app.state, "stems_dir", DEFAULT_STEMS_DIR)
    return Path(configured)


def _stem_roots(request: Request) -> tuple[Path, ...] | None:
    """Return an explicit mode boundary when the production app configured one."""

    configured = getattr(request.app.state, "stem_roots", None)
    if configured is None:
        return None
    return tuple(Path(root) for root in configured)


def _load_or_http_error(stable_id: str, request: Request) -> StemBundle:
    try:
        return load_stem_bundle(
            stable_id,
            stems_dir=_stems_dir(request),
            roots=_stem_roots(request),
        )
    except StemBundleNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "STEM_BUNDLE_NOT_FOUND", "message": str(exc)},
        ) from exc
    except StemArtifactError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "STEM_ARTIFACT_INVALID", "message": str(exc)},
        ) from exc


def _unavailable_out(stable_id: str, exc: StemBundleNotFoundError) -> StemUnavailableOut:
    return StemUnavailableOut(stable_id=stable_id, message=str(exc))


# ---------------------------------------------------------------------------
# On-demand R2 hydration (ADR-0024). Wiring is OPTIONAL: a machine with no
# CloudConfig / S3 client bound to app.state (local mode, or no R2 creds)
# simply never enqueues -- the routes fall back to the pre-existing
# unavailable/404 behavior unchanged. See apps.webui.server.app_wiring.
# ---------------------------------------------------------------------------


def _hydration_deps(request: Request) -> tuple[CloudConfig, AssetS3Client, Path] | None:
    cfg = getattr(request.app.state, "stem_hydration_cfg", None)
    s3 = getattr(request.app.state, "stem_hydration_s3", None)
    data_dir = getattr(request.app.state, "stem_hydration_data_dir", None)
    if cfg is None or s3 is None or data_dir is None:
        return None
    return cfg, s3, Path(data_dir)


def _run_hydration(
    stable_id: str,
    *,
    cfg: CloudConfig,
    s3: AssetS3Client,
    index: stem_index.StemAssetIndex,
    data_dir: Path,
    stems_dir: Path,
) -> stem_hydration.HydrationOutcome:
    outcome = stem_hydration.hydrate_one(
        stable_id,
        data_dir=data_dir,
        cfg=cfg,
        s3=s3,
        index=index,
        stems_dir=stems_dir,
        skip_reserved=False,  # on-demand deck load NEVER skips reserved ids (D5)
    )
    with _INFLIGHT_LOCK:
        if outcome.status == "error":
            _LAST_HYDRATE_ERROR[stable_id] = outcome.reason or "hydration failed"
        else:
            _LAST_HYDRATE_ERROR.pop(stable_id, None)
    return outcome


def _enqueue_hydration(stable_id: str, request: Request) -> Future | None:
    """Start background hydration if R2 has this bundle indexed.

    Returns the in-flight future, or ``None`` when there is nothing to
    hydrate -- either hydration is not configured on this machine, or the
    bundle is not in the (local-cache copy of the) R2 index. The index
    lookup here reads ONLY the local cache file (no network), so this is
    always cheap enough to call from the manifest route without blocking it.
    """
    deps = _hydration_deps(request)
    if deps is None:
        return None
    cfg, s3, data_dir = deps
    with _INFLIGHT_LOCK:
        existing = _INFLIGHT.get(stable_id)
        if existing is not None and not existing.done():
            return existing
        # Corrupt cache must not crash deck-load: treat as "nothing indexed
        # this request" and fall back to ordinary unavailable. The shared
        # loader raises on corrupt files; callers that need repair must catch.
        try:
            index = stem_index.load_cached_index(data_dir)
        except stem_index.StemIndexError:
            return None
        if stable_id not in index:
            return None
        stems_dir = _stems_dir(request)
        future = _HYDRATE_EXECUTOR.submit(
            _run_hydration,
            stable_id,
            cfg=cfg,
            s3=s3,
            index=index,
            data_dir=data_dir,
            stems_dir=stems_dir,
        )
        _INFLIGHT[stable_id] = future
        return future


def _recorded_hydration_error(stable_id: str) -> str | None:
    with _INFLIGHT_LOCK:
        return _LAST_HYDRATE_ERROR.get(stable_id)


def _raise_hydration_failed(reason: str) -> None:
    """Fail LOUD, never silent-empty: the index said this bundle exists, and
    hydration could not produce it. A caller must never read this the same
    as "no bundle anywhere" (HTTP 200 unavailable / 404) -- see the storage
    view's fail-loud requirement (ADR-0024)."""
    raise HTTPException(
        status_code=502,
        detail={"code": "STEM_BUNDLE_HYDRATION_FAILED", "message": reason},
    )


def _manifest_out(bundle: StemBundle) -> StemManifestOut:
    """Expose only the stable client contract after internal provenance checks."""
    return StemManifestOut(
        stable_id=bundle.manifest.stable_id,
        # The separator that actually made these files. Reporting "demucs" for
        # a RoFormer bundle would put a wrong model name in front of the user.
        source="demucs" if bundle.layout == "demucs4" else "roformer",
        model=bundle.manifest.model.name,
        layout=bundle.layout,
        sample_rate_hz=bundle.alignment.sample_rate,
        frame_count=bundle.alignment.frame_count,
        channel_count=bundle.alignment.channels,
        # The bundle's OWN parts, not a fixed four: a 2-part RoFormer bundle
        # must advertise exactly what it has so the client can render the
        # controls it cannot drive as inert rather than silently dead.
        parts={
            part: StemPartOut(media_type=bundle.media_type) for part in bundle.parts
        },
    )


def _open_validated_stem(path: Path) -> BufferedReader:
    """Open one validated file without a pathname-reopen race."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened_stat = os.fstat(descriptor)
        current_stat = path.lstat()
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or not stat.S_ISREG(current_stat.st_mode)
            or not os.path.samestat(opened_stat, current_stat)
        ):
            raise StemArtifactError("stem file changed after artifact validation")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _stream_file(source: BufferedReader) -> Iterator[bytes]:
    # Sync route -> each chunk is a separate anyio threadpool round trip. 1 MiB
    # meant 30-40 GIL handoffs per stem file; under N-deck-parallel loads those
    # compete and serialize throughput well below disk speed. 8 MiB cuts the
    # round trips ~8x with no behavior change.
    with source:
        while chunk := source.read(8 * 1024 * 1024):
            yield chunk


@router.get(
    "/{stable_id}/stems",
    response_model=StemManifestOut | StemUnavailableOut,
)
def get_stem_manifest(
    stable_id: str, request: Request
) -> StemManifestOut | StemUnavailableOut:
    """Return a stored v1 manifest after alignment, or HTTP 200 unavailable when none exists.

    Never blocks on R2: if the bundle is missing locally but the R2 index
    has it, this ENQUEUES a background hydration and returns immediately
    with ``hydrating=True`` (D2). A prior hydration attempt that already
    failed is reported LOUD as HTTP 502, never silently folded into the
    ordinary "no bundle" empty state.
    """
    try:
        bundle = load_stem_bundle(
            stable_id,
            stems_dir=_stems_dir(request),
            roots=_stem_roots(request),
        )
    except StemBundleNotFoundError as exc:
        recorded_error = _recorded_hydration_error(stable_id)
        if recorded_error is not None:
            _raise_hydration_failed(recorded_error)
        future = _enqueue_hydration(stable_id, request)
        if future is not None:
            return StemUnavailableOut(
                stable_id=stable_id,
                code="STEM_BUNDLE_HYDRATING",
                message="bundle not local yet; fetching from R2",
                hydrating=True,
            )
        return _unavailable_out(stable_id, exc)
    except StemArtifactError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "STEM_ARTIFACT_INVALID", "message": str(exc)},
        ) from exc
    return _manifest_out(bundle)


@router.get("/{stable_id}/stems/{part}", response_class=StreamingResponse)
def get_stem_file(stable_id: str, part: str, request: Request) -> StreamingResponse:
    """Stream one validated stem from its already-verified file handle.

    Unlike the manifest route, this one DOES wait (bounded) for an in-flight
    hydration: a part request means the caller already decided it wants
    these bytes. A prior failure is surfaced immediately as HTTP 502; a
    fresh attempt that does not finish within
    :data:`STEM_PART_HYDRATE_WAIT_S` answers HTTP 503 so the caller can
    retry rather than hang forever on this one request.
    """
    try:
        bundle = _load_or_http_error(stable_id, request)
    except HTTPException as http_exc:
        if http_exc.status_code != 404:
            raise
        recorded_error = _recorded_hydration_error(stable_id)
        if recorded_error is not None:
            _raise_hydration_failed(recorded_error)
        future = _enqueue_hydration(stable_id, request)
        if future is None:
            raise
        try:
            outcome = future.result(timeout=STEM_PART_HYDRATE_WAIT_S)
        except FutureTimeoutError:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "STEM_BUNDLE_HYDRATING",
                    "message": "still fetching from R2; retry shortly",
                },
            ) from None
        if outcome.status == "error":
            _raise_hydration_failed(outcome.reason or "hydration failed")
        bundle = _load_or_http_error(stable_id, request)
    if part not in bundle.parts:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "STEM_PART_NOT_FOUND",
                "message": (
                    f"unknown stem part {part!r} for a {bundle.layout} bundle; "
                    f"it has {bundle.parts}"
                ),
            },
        )
    try:
        source = _open_validated_stem(bundle.files[part])
    except (OSError, StemArtifactError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "STEM_ARTIFACT_INVALID", "message": str(exc)},
        ) from exc
    return StreamingResponse(
        _stream_file(source),
        media_type=bundle.media_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Length": str(os.fstat(source.fileno()).st_size),
        },
    )


@router.post("/{stable_id}/stems/deck-open")
def mark_stem_deck_open(stable_id: str) -> dict[str, str]:
    """A deck has this bundle open. Protects it from eviction until closed.

    Agent-native parity for the deck-load lifecycle: there is no other
    request boundary that tells the eviction path "a deck is playing this
    right now, do not free its bundle regardless of recency" (D3). Refcounted
    (:class:`apps.cloud.stem_hydration.OpenDeckRegistry`), so two open
    references (e.g. two decks on the same track) both need closing.
    """
    stem_hydration.OPEN_DECKS.mark_open(stable_id)
    return {"status": "ok", "stable_id": stable_id, "open": "true"}


@router.post("/{stable_id}/stems/deck-close")
def mark_stem_deck_closed(stable_id: str) -> dict[str, str]:
    """Release one deck-open reference. See :func:`mark_stem_deck_open`."""
    stem_hydration.OPEN_DECKS.mark_closed(stable_id)
    return {"status": "ok", "stable_id": stable_id, "open": "false"}


__all__ = [
    "STEM_PART_HYDRATE_WAIT_S",
    "StemManifestOut",
    "StemPartOut",
    "StemUnavailableOut",
    "router",
]
