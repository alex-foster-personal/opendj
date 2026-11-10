"""Stem manifest and part streaming helpers for stems routes.

Split from ``stems.py`` for issue #9676.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from concurrent.futures import TimeoutError as FutureTimeoutError
from io import BufferedReader
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from apps.cloud import stem_index
from apps.shared.shared_read_open import open_read_shared
from apps.stems.artifacts import StemArtifactError, StemBundle, StemBundleNotFoundError, load_stem_bundle

from .stems_bundle import (
    _load_or_http_error,
    _read_barrier,
    _stem_roots,
    _stems_dir,
    _unavailable_out,
)
from .stems_hydration import (
    STEM_PART_HYDRATE_WAIT_S,
    _enqueue_hydration,
    _HydrateError,
    _raise_hydrate_error,
    _raise_hydration_failed,
    _raise_stem_index_corrupt,
    _recorded_hydration_error,
    _stem_source_error_from_hub_reason,
)
from .stems_models import StemManifestOut, StemPartOut, StemUnavailableOut
from .stems_state import _fetching_progress_suffixes, _shared_hydration_dir, _state_progress


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


def _raise_stem_file_changed_after_validation() -> None:
    raise StemArtifactError("stem file changed after artifact validation")


def _open_validated_stem(path: Path) -> BufferedReader:
    """Open one validated file without a pathname-reopen race.

    The descriptor never blocks a later swap from renaming the bundle or
    deleting its backup, on Windows too (:func:`open_read_shared`).
    """
    descriptor = open_read_shared(path)
    try:
        opened_stat = os.fstat(descriptor)
        current_stat = path.lstat()
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or not stat.S_ISREG(current_stat.st_mode)
            or not os.path.samestat(opened_stat, current_stat)
        ):
            _raise_stem_file_changed_after_validation()
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


def _stem_manifest_response(stable_id: str, request: Request) -> StemManifestOut | StemUnavailableOut:
    try:
        with _read_barrier(stable_id, request):
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
            shared = _shared_hydration_dir(stable_id, request)
            sfx = _fetching_progress_suffixes(stable_id, request)
            stems_dir = _stems_dir(request)
            try:
                progress = _state_progress(
                    stable_id,
                    stems_dir,
                    shared,
                    attempt_suffixes=sfx,
                )
            except stem_index.StemIndexError as exc:
                _raise_stem_index_corrupt(exc, Path(shared))
            return StemUnavailableOut(
                stable_id=stable_id,
                code="STEM_BUNDLE_HYDRATING",
                message="bundle not local yet; fetching from R2",
                hydrating=True,
                progress=progress,
            )
        return _unavailable_out(stable_id, exc)
    except StemArtifactError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "STEM_ARTIFACT_INVALID", "message": str(exc)},
        ) from exc
    return _manifest_out(bundle)
def _open_stem_part(stable_id: str, part: str, request: Request) -> tuple[StemBundle, BufferedReader]:
    """Resolve the bundle and open ``part`` inside one :func:`_read_barrier` hold.

    The opened descriptor keeps the file a swap later moves away, so the
    stream that follows needs no lock.
    """
    with _read_barrier(stable_id, request):
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
            return bundle, _open_validated_stem(bundle.files[part])
        except (OSError, StemArtifactError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "STEM_ARTIFACT_INVALID", "message": str(exc)},
            ) from exc


def _http_error_code(exc: HTTPException) -> str | None:
    detail = exc.detail
    return detail.get("code") if isinstance(detail, dict) else None


def _stem_file_response(stable_id: str, part: str, request: Request) -> StreamingResponse:
    try:
        bundle, source = _open_stem_part(stable_id, part, request)
    except HTTPException as http_exc:
        # Only a missing bundle can be hydrated: a local bundle without the
        # asked part keeps its own 404 STEM_PART_NOT_FOUND (Codex on #6821).
        if _http_error_code(http_exc) != "STEM_BUNDLE_NOT_FOUND":
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
                _raise_hydrate_error(recorded)
            _raise_hydration_failed(str(exc))
        if outcome.status == "hub_error":
            hub_exc = _stem_source_error_from_hub_reason(
                outcome.reason or "hub transport failure"
            )
            _raise_hydrate_error(_HydrateError(hub_exc.code, hub_exc.message))
        if outcome.status == "error":
            recorded = _recorded_hydration_error(stable_id)
            if recorded is not None:
                _raise_hydrate_error(recorded)
            _raise_hydration_failed(outcome.reason or "hydration failed")
        bundle, source = _open_stem_part(stable_id, part, request)
    return StreamingResponse(
        _stream_file(source),
        media_type=bundle.media_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Length": str(os.fstat(source.fileno()).st_size),
        },
    )
