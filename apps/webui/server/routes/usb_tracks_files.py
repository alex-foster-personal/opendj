"""Stick file-serving routes (audio and artwork) split from usb_tracks for the 600-line limit."""

from __future__ import annotations

import errno

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from apps.shared import audio_quality
from apps.shared.bounded_file_open import AUDIO_ACCESS_TIMEOUT_S, probe_readable_byte
from apps.sync.usb.stick_file_pin import pin_stick_file
from apps.sync.usb.stick_library import (
    ArtworkSize,
    StickError,
    stick_artwork_file,
    stick_audio_file,
)

from .rb_assets_audio import _BLOCKED_ACCESS_ERRNOS
from .usb_tracks import (
    _CACHE_ARTWORK,
    _CACHE_AUDIO,
    _STICK_RESPONSES,
    _duration_ms,
    _resolve_track,
    _stick_errors,
    router,
)


@router.get(
    "/tracks/{track_id}/audio",
    response_class=FileResponse,
    responses=_STICK_RESPONSES,
    operation_id="get_usb_track_audio_api_v1_usb_tracks__track_id__audio_get",
)
@router.head(
    "/tracks/{track_id}/audio",
    response_class=FileResponse,
    responses=_STICK_RESPONSES,
    operation_id="head_usb_track_audio_api_v1_usb_tracks__track_id__audio_head",
)
def get_usb_track_audio(track_id: str, request: Request) -> FileResponse:
    """Stream the stick's file. FileResponse handles Range/206 and HEAD, as
    the library's ``/tracks/{id}/audio`` does.

    A subprocess probe opens the file under ``AUDIO_ACCESS_TIMEOUT_S`` first,
    so a kernel-blocked ``open()`` (a pending macOS Removable Volumes prompt)
    answers 503 ``AUDIO_ACCESS_BLOCKED`` instead of hanging the worker.
    """
    with _stick_errors():
        resolved = _resolve_track(request, track_id)
        audio = stick_audio_file(resolved)
    probe = probe_readable_byte(audio.path, timeout_s=AUDIO_ACCESS_TIMEOUT_S)
    if probe.outcome == "timeout" or (
        probe.outcome == "error" and probe.errno in _BLOCKED_ACCESS_ERRNOS
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "AUDIO_ACCESS_BLOCKED",
                "message": (
                    f"opening {audio.path} was refused or did not return within "
                    f"{AUDIO_ACCESS_TIMEOUT_S:.0f}s ({probe.outcome})"
                ),
                "volume_uuid": resolved.stick.volume_uuid,
            },
        )
    if probe.outcome == "error" and probe.errno in (errno.ENOENT, errno.ENOTDIR):
        raise HTTPException(
            status_code=404,
            detail=StickError(
                "USB_FILE_MISSING",
                f"audio file {resolved.track.file_path} went away before it could be opened",
                volume_uuid=resolved.stick.volume_uuid,
            ).to_detail(),
        )
    if probe.outcome == "error":
        raise OSError(probe.errno or 0, f"probing {audio.path} failed: {probe.message}")
    quality = audio_quality.classify(str(audio.path), _duration_ms(resolved.track.duration_s))
    with _stick_errors():
        pinned = pin_stick_file(resolved.stick.volume_uuid, audio.path)
    return FileResponse(
        pinned.serve_path,
        media_type=audio.media_type,
        headers={
            "Cache-Control": _CACHE_AUDIO,
            "X-Audio-Kind": "local",
            "X-Audio-Venue": quality.venue.key if quality.venue else "",
            "X-Audio-Source": "usb-stick",
        },
        background=BackgroundTask(pinned.close),
    )


@router.get(
    "/tracks/{track_id}/artwork",
    response_class=FileResponse,
    response_model=None,
    responses=_STICK_RESPONSES,
)
def get_usb_track_artwork(
    track_id: str,
    request: Request,
    size: ArtworkSize = Query(  # noqa: B008  # FastAPI DI
        "s", description="s=80x80 browser rows; m and orig = the 240x240 _m jpg"
    ),
) -> FileResponse:
    """The pdb's pre-rendered jpg: ``s`` as named, ``m``/``orig`` its ``_m``
    sibling (the largest rendering rekordbox writes to a stick)."""
    with _stick_errors():
        resolved = _resolve_track(request, track_id)
        pinned = pin_stick_file(resolved.stick.volume_uuid, stick_artwork_file(resolved, size))
    return FileResponse(
        pinned.serve_path,
        media_type="image/jpeg",
        headers={"Cache-Control": _CACHE_ARTWORK},
        background=BackgroundTask(pinned.close),
    )
