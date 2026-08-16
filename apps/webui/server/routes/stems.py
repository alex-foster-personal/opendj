"""Read-only endpoints for validated precomputed Demucs stem bundles.

The application integrator mounts :data:`router` at ``/api/v1``.  The router
does not run Demucs or mutate files: every request reloads and validates the
stored artifact before it exposes either the manifest or a WAV response.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from io import BufferedReader
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..stem_artifacts import (
    DEFAULT_STEMS_DIR,
    STEM_PARTS,
    StemArtifactError,
    StemBundle,
    StemBundleNotFoundError,
    load_stem_bundle,
)

router = APIRouter(prefix="/tracks", tags=["stems"])


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


def _stems_dir(request: Request) -> Path:
    """Use an injected directory in isolated apps, else the canonical state dir."""
    configured = getattr(request.app.state, "stems_dir", DEFAULT_STEMS_DIR)
    return Path(configured)


def _load_or_http_error(stable_id: str, request: Request) -> StemBundle:
    try:
        return load_stem_bundle(stable_id, stems_dir=_stems_dir(request))
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


@router.get("/{stable_id}/stems", response_model=StemManifestOut)
def get_stem_manifest(stable_id: str, request: Request) -> StemManifestOut:
    """Return a stored v1 manifest only after all four files prove alignment."""
    bundle = _load_or_http_error(stable_id, request)
    return _manifest_out(bundle)


@router.get("/{stable_id}/stems/{part}", response_class=StreamingResponse)
def get_stem_file(stable_id: str, part: str, request: Request) -> StreamingResponse:
    """Stream one validated WAV stem from its already-verified file handle."""
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


__all__ = ["StemManifestOut", "StemPartOut", "router"]
