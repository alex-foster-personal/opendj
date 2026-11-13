"""``GET /playlists/{playlist_id}/export.m3u8``: a playlist as an extended M3U8 file (LIBORG-9748).

Its own router so ``routes/playlists.py`` stays untouched; the module name sorts before
``playlists`` so this path registers ahead of that router's ``/{playlist_id}`` routes.
Agent parity: ``opendj playlist export <id> --format m3u8 [--out FILE]``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from ..backend import StateBackend
from ..deps import get_read_state
from ..playlist_m3u8_export import (
    M3U8_EXPORT_MEDIA_TYPE,
    M3u8ExportTrack,
    local_track_path,
    m3u8_export_filename,
    playlist_m3u8_export,
)

router = APIRouter(prefix="/playlists", tags=["playlists"])
MOUNT_UNDER_API_PREFIX = True


@router.get(
    "/{playlist_id}/export.m3u8",
    response_class=Response,
    responses={
        200: {
            "content": {"audio/x-mpegurl": {}},
            "headers": {
                "X-Skipped": {"description": "Tracks left out for having no local file", "schema": {"type": "integer"}}
            },
        }
    },
    operation_id="export_playlist_m3u8",
)
def export_playlist_m3u8(
    playlist_id: str,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> Response:
    """A playlist as UTF-8 extended M3U8: EXTINF and absolute path per local track, in playlist order."""
    pl = backend.get_playlist(playlist_id)
    tracks_map = backend.get_tracks_bulk(list(pl.items))
    missing = [sid for sid in pl.items if sid not in tracks_map]
    if missing:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "PLAYLIST_MEMBER_MISSING",
                "message": f"playlist {playlist_id} references {len(missing)} stable_ids with no track row (first: {missing[:5]})",
            },
        )
    export = playlist_m3u8_export(
        M3u8ExportTrack(
            title=t.title,
            artist=t.artist,
            duration_ms=t.duration_ms,
            local_path=local_track_path(t.file_path),
        )
        for t in (tracks_map[sid] for sid in pl.items)
    )
    return Response(
        content=export.body,
        media_type=M3U8_EXPORT_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{m3u8_export_filename(pl.name)}"',
            "X-Skipped": str(export.skipped),
        },
    )
