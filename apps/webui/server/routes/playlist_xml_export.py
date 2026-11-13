"""``GET /playlists/{playlist_id}/export.xml``: one playlist as a rekordbox XML file (LIBORG-9751).

The whole-collection export (``GET /library/export.xml``, LIBORG-10051) narrowed to one
playlist: its tracks in COLLECTION, once each, and ROOT holding just that playlist. Its own
router so ``routes/playlists.py`` stays untouched; the module name sorts before ``playlists``
so this path registers ahead of that router's ``/{playlist_id}`` routes.
Agent parity: ``opendj playlist export <id> --format rekordbox-xml [--out FILE]``.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from apps.adapters.rekordbox_xml.library_export import ExportRefused, PlaylistNotFound, plan_playlist_export
from apps.shared.state.db import open_ro

from ..analysis_overlay import lane_owned_fields
from ..deps import get_library_state_db
from ..playlist_text_export import export_filename

router = APIRouter(prefix="/playlists", tags=["playlists"])
MOUNT_UNDER_API_PREFIX = True


@router.get(
    "/{playlist_id}/export.xml",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/xml": {}}}},
    operation_id="export_playlist_rekordbox_xml",
)
def export_playlist_xml(
    playlist_id: str,
    state_db: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> StreamingResponse:
    """One playlist as rekordbox XML that rekordbox and the Open DJ reader import.

    [if] the state DB is missing [then] 503 STATE_DB_MISSING
    [if] no live playlist has the id [then] 404 PLAYLIST_NOT_FOUND
    [if] the id is a folder or a set [then] 409 PLAYLIST_EXPORT_REFUSED
    """
    try:
        conn = open_ro(state_db, check_same_thread=False)
    except FileNotFoundError as exc:
        raise HTTPException(503, detail={"code": "STATE_DB_MISSING", "message": str(exc)}) from exc
    try:
        export = plan_playlist_export(conn, playlist_id, lane_fields=partial(lane_owned_fields, state_db_path=state_db))
    except PlaylistNotFound as exc:
        raise HTTPException(404, detail={"code": "PLAYLIST_NOT_FOUND", "message": str(exc)}) from exc
    except ExportRefused as exc:
        raise HTTPException(409, detail={"code": "PLAYLIST_EXPORT_REFUSED", "message": str(exc)}) from exc
    filename = export_filename(export.tree[0].name).removesuffix(".txt") + ".xml"
    return StreamingResponse(
        export.iter_bytes(),
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Export-Tracks": str(export.track_count),
            "X-Export-Entries": str(export.entry_count),
            "X-Export-Skipped-No-Location": str(export.skipped_no_location),
            "X-Export-Skipped-Streaming": str(export.skipped_streaming),
        },
    )
