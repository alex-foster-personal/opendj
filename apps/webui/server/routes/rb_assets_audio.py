"""HEAD/GET audio stream route split from rb_assets (quality file-size ratchet)."""

from __future__ import annotations

import errno

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse

from apps.shared.bounded_file_open import AUDIO_ACCESS_TIMEOUT_S, probe_readable_byte

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import get_read_state
from .rb_assets import _CACHE_AUDIO, router

_BLOCKED_ACCESS_ERRNOS: frozenset[int] = frozenset({errno.EACCES, errno.EPERM})

_AUDIO_BLOCKED_RESPONSES: dict[int | str, dict[str, object]] = {
    503: {
        "description": (
            "AUDIO_ACCESS_BLOCKED: opening the resolved library-media path did not "
            "return within AUDIO_ACCESS_TIMEOUT_S (kernel-blocked open, e.g. FIFO "
            "with no writer or macOS Media Library denial)"
        )
    }
}


@router.api_route(
    "/{stable_id}/audio",
    methods=["GET", "HEAD"],
    response_class=FileResponse,
    responses=_AUDIO_BLOCKED_RESPONSES,
    # Pinned: FastAPI derives the default id from route.methods, a SET, so a
    # two-method route flips between _get and _head per hash seed and reds the
    # openapi.json contract-drift gate on the runner while matching locally.
    operation_id="get_track_audio_api_v1_tracks__stable_id__audio_get",
)
def get_track_audio(
    stable_id: str,
    request: Request,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008
) -> FileResponse:
    """Stream the single best working file. FileResponse handles Range/206.

    HEAD is served by the same handler: FileResponse sends headers only for a
    HEAD scope, and the LyricsPanel playability probe (``fetch(url,
    {method: 'HEAD'})``) read a GET-only route as "HTTP 405, audio cannot be
    played" on every track with aligned words (found Sat 12 Sep 2026, #2082).

    The backend picks among ``track_locations`` plus the legacy
    ``file_path`` / FolderPath. The frontend never sees the alternatives.
    Share-host requests use the share venue cap (lossy ceiling by default).

    Before streaming, a subprocess probe opens the resolved path under
    ``AUDIO_ACCESS_TIMEOUT_S`` so a kernel-blocked ``open()`` answers HTTP 503
    with ``AUDIO_ACCESS_BLOCKED`` instead of hanging the ASGI worker (#2749).
    """
    share = getattr(request.state, "share_audience", "local") == "share"
    jobs_store = getattr(request.app.state, "jobs_store", None)
    picked = rb_vendor.resolve_playable_audio(
        stable_id, share=share, jobs_store=jobs_store
    )
    probe = probe_readable_byte(picked.path, timeout_s=AUDIO_ACCESS_TIMEOUT_S)
    if probe.outcome == "timeout" or (
        probe.outcome == "error" and probe.errno in _BLOCKED_ACCESS_ERRNOS
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "AUDIO_ACCESS_BLOCKED",
                "message": (
                    f"opening {picked.path} did not return within "
                    f"{AUDIO_ACCESS_TIMEOUT_S:.0f}s"
                ),
            },
        )
    return FileResponse(
        picked.path,
        media_type=picked.media_type,
        headers={
            "Cache-Control": _CACHE_AUDIO,
            "X-Audio-Kind": picked.kind,
            "X-Audio-Venue": picked.venue_key or "",
            "X-Audio-Source": picked.source,
        },
    )
