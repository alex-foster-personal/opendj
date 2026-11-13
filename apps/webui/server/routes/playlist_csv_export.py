"""``GET /playlists/{playlist_id}/export.csv``: a playlist as a CSV set list (LIBM-114).

Its own router so ``routes/playlists.py`` stays untouched; the module name sorts before
``playlists`` so this path registers ahead of that router's ``/{playlist_id}`` routes.
Agent parity: ``opendj playlist export <id> --format csv [--out FILE]``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from ..backend import StateBackend
from ..deps import get_read_state
from ..playlist_csv_export import CSV_EXPORT_MEDIA_TYPE, CsvExportTrack, csv_export_filename, playlist_csv_export

router = APIRouter(prefix="/playlists", tags=["playlists"])
MOUNT_UNDER_API_PREFIX = True


@router.get(
    "/{playlist_id}/export.csv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}},
    operation_id="export_playlist_csv",
)
def export_playlist_csv(
    playlist_id: str,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> Response:
    """A playlist as UTF-8 (with BOM) CSV: header row, then one row per track in playlist order."""
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
    body = playlist_csv_export(
        CsvExportTrack(
            title=t.title,
            artist=t.artist,
            album=t.album,
            genre=t.genre,
            bpm=t.bpm,
            key=t.key,
            duration_ms=t.duration_ms,
            rating=t.rating,
            comment=t.comments,
            path=t.file_path,
        )
        for t in (tracks_map[sid] for sid in pl.items)
    )
    return Response(
        content=body,
        media_type=CSV_EXPORT_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{csv_export_filename(pl.name)}"'},
    )
