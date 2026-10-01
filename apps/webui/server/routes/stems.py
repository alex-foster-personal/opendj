"""Read-only endpoints for validated precomputed Demucs stem bundles.

The application integrator mounts :data:`router` at ``/api/v1``.  The router
does not run Demucs or mutate files: every request reloads and validates the
stored artifact before it exposes either the manifest or a container-typed audio response.
The manifest GET answers HTTP 200 unavailable when no bundle exists; the
part GET still 404s when there is no file to stream.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from io import BufferedReader
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from apps.cloud import stem_cache_budget, stem_hydration, stem_index
from apps.cloud.hub_stem_client import STEM_BUNDLE_PRESIGN_PATH, STEM_INDEX_PATH
from apps.cloud.stem_source import (
    STEM_BUNDLE_NOT_INDEXED,
    STEM_HUB_INDEX_FAILED,
    STEM_HUB_UNREACHABLE,
    DirectR2Source,
    StemHydrationSource,
    StemSourceError,
    hub_transport_failure_kind,
)
from apps.stems.artifacts import (
    DEFAULT_STEMS_DIR,
    StemArtifactError,
    StemBundle,
    StemBundleNotFoundError,
    load_stem_bundle,
)
from apps.webui.server.routes.sync_hub_route_errors import raise_sync_hub_unreachable

router = APIRouter(prefix="/tracks", tags=["stems"])

log = logging.getLogger(__name__)

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
#: stable_id -> number of files the index lists for the bundle being fetched,
#: recorded at enqueue so a progress read never has to re-parse the index.
_INFLIGHT_FILE_TOTALS: dict[str, int] = {}
#: stable_id -> reason, for a bundle the index says exists but whose last
#: hydration attempt failed. Consulted BEFORE re-enqueueing so a hot loop of
#: part requests does not hammer R2 with the same doomed fetch, and so a
#: request can fail loud immediately instead of waiting out the full timeout
#: again for an error already known.
@dataclass(frozen=True)
class _HydrateError:
    code: str
    message: str


_LAST_HYDRATE_ERROR: dict[str, _HydrateError] = {}

STEM_HYDRATION_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    502: {
        "description": (
            "Index-dependent hydration failure: STEM_BUNDLE_HYDRATION_FAILED when "
            "the bundle is indexed but cannot be fetched, STEM_INDEX_CORRUPT when "
            "the local index cache is unreadable, STEM_HYDRATION_NOT_ARMED when "
            "this engine is configured for hydration but could not arm it at boot "
            "(for example boto3 is absent or the hub was unreachable). "
            "Transient boot failures such as HTTP 403/5xx may self-recover on the "
            "next throttled stems miss; structural failures such as missing boto3, "
            "unusable sync credential, or HTTP 401 STEM_HUB_AUTH_REFUSED stay "
            "terminal until operator action. STEM_HUB_AUTH_REFUSED when the hub "
            "rejects the sync credential, or STEM_HUB_INDEX_FAILED when the hub "
            "index or presign path fails with a non-unreachable error"
        )
    },
    503: {
        "description": (
            "Configured sync hub unreachable (SYNC_HUB_UNREACHABLE with endpoint "
            "and underlying error)"
        )
    },
}

STEM_PART_RESPONSES: dict[int | str, dict[str, Any]] = {
    **STEM_HYDRATION_ERROR_RESPONSES,
    503: {
        "description": (
            "STEM_BUNDLE_HYDRATING: a fresh R2 hydration did not finish within "
            "STEM_PART_HYDRATE_WAIT_S; retry the request"
        )
    },
}


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


class StemHydrationProgressOut(BaseModel):
    """How far an in-flight cloud fetch has got, read from its temp directory."""

    model_config = ConfigDict(frozen=True)

    files_total: int
    files_done: int
    bytes_done: int


StemTrackState = Literal["local", "cloud", "fetching", "error", "none"]


class StemStateOut(BaseModel):
    """One track's stem bundle, named: where it is and what is happening to it.

    ``local`` is on this machine and playable; ``cloud`` is only in the R2
    index and a hydrate will fetch it; ``fetching`` is being downloaded now;
    ``error`` is a bundle that should exist and could not be produced;
    ``none`` is no bundle anywhere this engine can see.
    """

    model_config = ConfigDict(frozen=True)

    stable_id: str
    state: StemTrackState
    message: str
    hydration_armed: bool
    progress: StemHydrationProgressOut | None = None
    error_code: str | None = None
    deck_open: bool = False


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
    #: Set only while ``hydrating`` is true: files and bytes fetched so far.
    progress: StemHydrationProgressOut | None = None


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


def _hydration_deps(request: Request) -> tuple[StemHydrationSource, Path] | None:
    unarmed_reason = getattr(request.app.state, "stem_hydration_unarmed_reason", None)
    if unarmed_reason is not None:
        unarmed_kind = getattr(request.app.state, "stem_hydration_unarmed_kind", None)
        if unarmed_kind == "transient":
            from apps.webui.server.stem_hydration_rearm import maybe_rearm_stem_hydration

            if maybe_rearm_stem_hydration(request.app):
                unarmed_reason = None
            else:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "STEM_HYDRATION_NOT_ARMED",
                        "message": unarmed_reason,
                    },
                )
        else:
            raise HTTPException(
                status_code=502,
                detail={"code": "STEM_HYDRATION_NOT_ARMED", "message": unarmed_reason},
            )
    source = getattr(request.app.state, "stem_hydration_source", None)
    data_dir = getattr(request.app.state, "stem_hydration_data_dir", None)
    if source is None and getattr(request.app.state, "stem_hydration_cfg", None) is not None:
        cfg = request.app.state.stem_hydration_cfg
        s3 = getattr(request.app.state, "stem_hydration_s3", None)
        if s3 is not None:
            source = DirectR2Source(cfg=cfg, s3=s3)
    if source is None or data_dir is None:
        return None
    return source, Path(data_dir)


def _hub_endpoint_from_message(message: str) -> str:
    if "bundle presign" in message or "presign" in message.casefold():
        return STEM_BUNDLE_PRESIGN_PATH
    return STEM_INDEX_PATH


def _stem_source_error_from_hub_reason(reason: str) -> StemSourceError:
    for code in (STEM_HUB_UNREACHABLE, STEM_HUB_INDEX_FAILED):
        exc = StemSourceError(code, reason)
        if hub_transport_failure_kind(exc) is not None:
            return exc
    return StemSourceError(STEM_HUB_INDEX_FAILED, reason)


def _raise_hydrate_error(error: _HydrateError) -> None:
    exc = StemSourceError(error.code, error.message)
    if hub_transport_failure_kind(exc) == "unreachable":
        raise_sync_hub_unreachable(_hub_endpoint_from_message(error.message), exc)
    if hub_transport_failure_kind(exc) is not None:
        raise HTTPException(
            status_code=502,
            detail={"code": error.code, "message": error.message},
        ) from exc
    _raise_hydration_failed(error)


def _raise_source_error(exc: StemSourceError) -> None:
    if hub_transport_failure_kind(exc) == "unreachable":
        raise_sync_hub_unreachable(_hub_endpoint_from_message(exc.message), exc)
    raise HTTPException(
        status_code=502,
        detail={"code": exc.code, "message": exc.message},
    ) from exc


def _run_hydration(
    stable_id: str,
    *,
    source: StemHydrationSource,
    index: stem_index.StemAssetIndex,
    data_dir: Path,
    stems_dir: Path,
) -> stem_hydration.HydrationOutcome:
    try:
        outcome = stem_hydration.hydrate_one(
            stable_id,
            data_dir=data_dir,
            source=source,
            index=index,
            stems_dir=stems_dir,
            skip_reserved=False,  # on-demand deck load NEVER skips reserved ids (D5)
        )
    except StemSourceError as exc:
        with _INFLIGHT_LOCK:
            _LAST_HYDRATE_ERROR[stable_id] = _HydrateError(exc.code, exc.message)
        _log_hydration_failure(stable_id, exc.code, exc.message)
        return stem_hydration.HydrationOutcome(
            stable_id=stable_id,
            status="error",
            reason=exc.message,
        )
    except Exception as exc:
        with _INFLIGHT_LOCK:
            _LAST_HYDRATE_ERROR[stable_id] = _HydrateError(
                "STEM_BUNDLE_HYDRATION_FAILED",
                str(exc),
            )
        _log_hydration_failure(stable_id, "STEM_BUNDLE_HYDRATION_FAILED", str(exc))
        raise
    with _INFLIGHT_LOCK:
        if outcome.status == "error":
            _LAST_HYDRATE_ERROR[stable_id] = _HydrateError(
                "STEM_BUNDLE_HYDRATION_FAILED",
                outcome.reason or "hydration failed",
            )
        elif outcome.status == "hub_error":
            hub_exc = _stem_source_error_from_hub_reason(
                outcome.reason or "hub transport failure"
            )
            _LAST_HYDRATE_ERROR[stable_id] = _HydrateError(
                hub_exc.code,
                hub_exc.message,
            )
        else:
            _LAST_HYDRATE_ERROR.pop(stable_id, None)
    if outcome.status == "error":
        _log_hydration_failure(
            stable_id, "STEM_BUNDLE_HYDRATION_FAILED", outcome.reason or "hydration failed"
        )
    elif outcome.status == "hub_error":
        _log_hydration_failure(
            stable_id, hub_exc.code, outcome.reason or "hub transport failure"
        )
    return outcome


def _log_hydration_failure(stable_id: str, code: str, reason: str) -> None:
    """Report an indexed bundle that could not be fetched.

    The manifest route already answers this loud (502), but an HTTP error
    never becomes a Sentry event (``failed_request_status_codes=set()``), and
    the deck that asked may be mid-set, where the client report is held back.
    ERROR reaches the error sink and Sentry through the engine warning log;
    an unreachable hub stays WARNING, because a laptop offline at a venue is
    an expected state, not a defect.
    """
    exc = StemSourceError(code, reason)
    level = (
        logging.WARNING
        if hub_transport_failure_kind(exc) == "unreachable"
        else logging.ERROR
    )
    log.log(
        level,
        "stem-hydration: indexed bundle could not be fetched [%s] stable_id=%s: %s",
        code,
        stable_id,
        reason,
    )


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
    source, data_dir = deps
    cache_path = stem_index.local_index_cache_path(data_dir)
    if not cache_path.is_file():
        try:
            source.refresh_index(data_dir)
        except StemSourceError as exc:
            _raise_source_error(exc)
        except Exception as exc:
            _raise_index_refresh_failed(str(exc))
    with _INFLIGHT_LOCK:
        existing = _INFLIGHT.get(stable_id)
        if existing is not None and not existing.done():
            return existing
        # Corrupt cache must fail loud on the index-dependent miss path only.
        # A bundle that already loads locally never reaches this code.
        try:
            index = stem_index.load_cached_index(data_dir)
        except stem_index.StemIndexError as exc:
            _raise_stem_index_corrupt(exc, data_dir)
        if stable_id not in index:
            if not cache_path.is_file():
                refresh_err = source.refresh_error(data_dir)
                if refresh_err is not None:
                    _raise_index_refresh_failed(refresh_err)
            _raise_bundle_not_indexed(stable_id)
        stems_dir = _stems_dir(request)
        future = _HYDRATE_EXECUTOR.submit(
            _run_hydration,
            stable_id,
            source=source,
            index=index,
            data_dir=data_dir,
            stems_dir=stems_dir,
        )
        _INFLIGHT[stable_id] = future
        _INFLIGHT_FILE_TOTALS[stable_id] = len(index[stable_id])
        return future


def _recorded_hydration_error(stable_id: str) -> _HydrateError | None:
    with _INFLIGHT_LOCK:
        return _LAST_HYDRATE_ERROR.get(stable_id)


def _raise_hydration_failed(error: _HydrateError | str) -> None:
    """Fail LOUD, never silent-empty: the index said this bundle exists, and
    hydration could not produce it. A caller must never read this the same
    as "no bundle anywhere" (HTTP 200 unavailable / 404) -- see the storage
    view's fail-loud requirement (ADR-0024)."""
    if isinstance(error, str):
        detail = {"code": "STEM_BUNDLE_HYDRATION_FAILED", "message": error}
    else:
        detail = {"code": error.code, "message": error.message}
    raise HTTPException(status_code=502, detail=detail)


def _raise_bundle_not_indexed(stable_id: str) -> None:
    """Fail loud when hydration is armed but the index has no bundle entry."""
    raise HTTPException(
        status_code=502,
        detail={
            "code": STEM_BUNDLE_NOT_INDEXED,
            "message": f"stable_id {stable_id!r} is not in the published stem index",
        },
    )


def _raise_stem_index_corrupt(exc: stem_index.StemIndexError, data_dir: Path) -> None:
    """Fail loud when the local index cache exists but cannot be read.

    Only reached on the index-dependent miss path (no local bundle yet).
    A bundle that already loads locally never consults the index cache.
    """
    raise HTTPException(
        status_code=502,
        detail={
            "code": "STEM_INDEX_CORRUPT",
            "message": str(exc),
            "path": str(stem_index.local_index_cache_path(data_dir)),
        },
    ) from exc


def _raise_index_refresh_failed(reason: str) -> None:
    """Fail loud when a cold-cache single-flight refresh from R2 failed: the
    caller cannot tell 'nothing published yet' from 'refresh error' unless
    this is surfaced explicitly, never silently folded into the ordinary
    unavailable/404 empty state."""
    raise HTTPException(
        status_code=502,
        detail={"code": "STEM_INDEX_REFRESH_FAILED", "message": reason},
    )


def _hydration_progress(stable_id: str, stems_dir: Path) -> StemHydrationProgressOut:
    """Files and bytes the in-flight fetch has written so far.

    Read from the fetch's own temp directory (``<stable_id>.tmp-hydrate-*``),
    so it costs a directory listing and never touches the network or the
    fetch itself. A file still being written counts as done with the bytes it
    has, which is why this is progress and not a completion check.
    """
    files_done = 0
    bytes_done = 0
    prefix = f"{stable_id}{stem_cache_budget.IN_FLIGHT_MARKER}"
    if stems_dir.is_dir():
        for tmp_dir in stems_dir.iterdir():
            if not tmp_dir.name.startswith(prefix) or not tmp_dir.is_dir():
                continue
            for child in tmp_dir.iterdir():
                try:
                    child_stat = child.stat()
                except FileNotFoundError:
                    # The fetch published (renamed) the directory mid-listing.
                    continue
                if stat.S_ISREG(child_stat.st_mode):
                    files_done += 1
                    bytes_done += child_stat.st_size
    with _INFLIGHT_LOCK:
        files_total = _INFLIGHT_FILE_TOTALS.get(stable_id, 0)
    return StemHydrationProgressOut(
        files_total=files_total, files_done=files_done, bytes_done=bytes_done
    )


def _hydration_in_flight(stable_id: str) -> bool:
    with _INFLIGHT_LOCK:
        future = _INFLIGHT.get(stable_id)
        return future is not None and not future.done()


def _stem_state(stable_id: str, request: Request) -> StemStateOut:  # noqa: PLR0911 - one return per named state
    """Name the track's stem state WITHOUT starting or re-arming anything.

    Deliberately reads ``app.state`` directly instead of calling
    :func:`_hydration_deps`, which raises and may re-arm hydration: a state
    read that changes the state it reports is not a read.
    """
    stems_dir = _stems_dir(request)
    deck_open = stable_id in stem_hydration.OPEN_DECKS.open_ids()
    state = request.app.state
    unarmed_reason = getattr(state, "stem_hydration_unarmed_reason", None)
    data_dir = getattr(state, "stem_hydration_data_dir", None)
    has_source = (
        getattr(state, "stem_hydration_source", None) is not None
        or (
            getattr(state, "stem_hydration_cfg", None) is not None
            and getattr(state, "stem_hydration_s3", None) is not None
        )
    )
    armed = unarmed_reason is None and has_source and data_dir is not None

    def _out(
        name: StemTrackState,
        message: str,
        *,
        progress: StemHydrationProgressOut | None = None,
        error_code: str | None = None,
    ) -> StemStateOut:
        return StemStateOut(
            stable_id=stable_id,
            state=name,
            message=message,
            hydration_armed=armed,
            progress=progress,
            error_code=error_code,
            deck_open=deck_open,
        )

    try:
        load_stem_bundle(stable_id, stems_dir=stems_dir, roots=_stem_roots(request))
    except StemBundleNotFoundError:
        pass
    except StemArtifactError as exc:
        return _out("error", str(exc), error_code="STEM_ARTIFACT_INVALID")
    else:
        return _out("local", "stem bundle is on this machine")

    if _hydration_in_flight(stable_id):
        return _out(
            "fetching",
            "fetching the stem bundle from the cloud",
            progress=_hydration_progress(stable_id, stems_dir),
        )
    recorded = _recorded_hydration_error(stable_id)
    if recorded is not None:
        return _out("error", recorded.message, error_code=recorded.code)
    if unarmed_reason is not None:
        return _out("error", unarmed_reason, error_code="STEM_HYDRATION_NOT_ARMED")
    if not armed:
        return _out(
            "none",
            "no stem bundle on this machine, and cloud stems are not configured here",
        )
    try:
        index = stem_index.load_cached_index(Path(data_dir))
    except stem_index.StemIndexError as exc:
        return _out("error", str(exc), error_code="STEM_INDEX_CORRUPT")
    if stable_id in index:
        return _out("cloud", "stem bundle is in the cloud and is fetched on demand")
    return _out("none", "no stem bundle on this machine or in the cloud")


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
    responses=STEM_HYDRATION_ERROR_RESPONSES,
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
            _raise_hydrate_error(recorded_error)
        future = _enqueue_hydration(stable_id, request)
        if future is not None:
            return StemUnavailableOut(
                stable_id=stable_id,
                code="STEM_BUNDLE_HYDRATING",
                message="bundle not local yet; fetching from R2",
                hydrating=True,
                progress=_hydration_progress(stable_id, _stems_dir(request)),
            )
        return _unavailable_out(stable_id, exc)
    except StemArtifactError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "STEM_ARTIFACT_INVALID", "message": str(exc)},
        ) from exc
    stem_hydration.OPEN_DECKS.mark_served(stable_id)
    return _manifest_out(bundle)


@router.get("/{stable_id}/stems/state", response_model=StemStateOut)
def get_stem_state(stable_id: str, request: Request) -> StemStateOut:
    """Name this track's stem state. Read-only: never starts a download.

    Agent-native parity for what the deck's stem row shows. Registered BEFORE
    the ``{part}`` route on purpose, which would otherwise read ``state`` as a
    stem part name.
    """
    return _stem_state(stable_id, request)


@router.post(
    "/{stable_id}/stems/hydrate",
    response_model=StemStateOut,
    responses=STEM_HYDRATION_ERROR_RESPONSES,
)
def hydrate_stem_bundle(stable_id: str, request: Request) -> StemStateOut:
    """Fetch this track's stem bundle from the cloud now, and retry a failure.

    The manifest route records a failed fetch and answers 502 from then on so
    a hot loop cannot hammer R2 with the same doomed request. This is the
    explicit way back: it drops that record and starts a fresh fetch through
    the same single-flight path, so the cache floor and the evictor apply
    exactly as they do for a deck load. A bundle already on disk is a no-op.
    Returns at once with the resulting state; poll ``.../stems/state``.
    """
    stems_dir = _stems_dir(request)
    try:
        load_stem_bundle(stable_id, stems_dir=stems_dir, roots=_stem_roots(request))
    except StemBundleNotFoundError:
        with _INFLIGHT_LOCK:
            _LAST_HYDRATE_ERROR.pop(stable_id, None)
        _enqueue_hydration(stable_id, request)
    except StemArtifactError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "STEM_ARTIFACT_INVALID", "message": str(exc)},
        ) from exc
    return _stem_state(stable_id, request)


@router.get(
    "/{stable_id}/stems/{part}",
    response_class=StreamingResponse,
    responses=STEM_PART_RESPONSES,
)
def get_stem_file(stable_id: str, part: str, request: Request) -> StreamingResponse:
    """Stream one validated stem part using the bundle's container media type.

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
            _raise_hydrate_error(recorded_error)
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
        except Exception as exc:  # noqa: BLE001 - any escaping exception from the hydration future must still answer the same 502 contract, never an unclassified 500
            recorded = _recorded_hydration_error(stable_id)
            if recorded is not None:
                _raise_hydration_failed(recorded)
            _raise_hydration_failed(str(exc))
        if outcome.status == "hub_error":
            hub_exc = _stem_source_error_from_hub_reason(
                outcome.reason or "hub transport failure"
            )
            _raise_hydrate_error(_HydrateError(hub_exc.code, hub_exc.message))
        if outcome.status == "error":
            recorded = _recorded_hydration_error(stable_id)
            if recorded is not None:
                _raise_hydration_failed(recorded)
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
    stem_hydration.OPEN_DECKS.mark_served(stable_id)
    return StreamingResponse(
        _stream_file(source),
        media_type=bundle.media_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Length": str(os.fstat(source.fileno()).st_size),
        },
    )


class StemWaveformOut(BaseModel):
    """Mono peak envelope for one stem part (issue #1036)."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    schema_version: int = Field(default=1, alias="schema")
    stable_id: str
    part: str
    layout: str
    points: int
    envelope: list[float]


def _stem_waveform_cache_root(request: Request) -> Path:
    configured = getattr(request.app.state, "data_dir", None)
    if configured is not None:
        return Path(configured) / "state" / "stem-waveform-cache"
    root = Path(os.environ.get("MDT_DATA_DIR", "data"))
    return root / "state" / "stem-waveform-cache"


@router.get(
    "/{stable_id}/stems/{part}/waveform",
    response_model=StemWaveformOut,
    responses=STEM_PART_RESPONSES,
)
def get_stem_waveform(stable_id: str, part: str, request: Request) -> StemWaveformOut:
    """Return a downsampled mono peak envelope for one validated stem part."""
    from apps.stems.stem_waveform import load_stem_waveform_payload

    try:
        payload = load_stem_waveform_payload(
            stable_id,
            part,
            stems_dir=_stems_dir(request),
            cache_root=_stem_waveform_cache_root(request),
            roots=_stem_roots(request),
        )
    except StemBundleNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "STEM_BUNDLE_NOT_FOUND", "message": str(exc)},
        ) from exc
    except StemArtifactError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "STEM_PART_NOT_FOUND", "message": str(exc)},
        ) from exc
    stem_hydration.OPEN_DECKS.mark_served(stable_id)
    return StemWaveformOut(**payload)


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
    "STEM_HYDRATION_ERROR_RESPONSES",
    "STEM_PART_HYDRATE_WAIT_S",
    "StemHydrationProgressOut",
    "StemManifestOut",
    "StemPartOut",
    "StemStateOut",
    "StemUnavailableOut",
    "StemWaveformOut",
    "router",
]
