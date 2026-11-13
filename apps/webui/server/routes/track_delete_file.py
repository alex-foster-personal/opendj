"""Delete a track in two additive tiers (LIBM-53).

  POST /tracks/{stable_id}:delete  {remove_from_collection, delete_file, confirm: true} + If-Match

One request, two strictly additive tiers: ``remove_from_collection``, then also
``delete_file``. ``delete_file`` without ``remove_from_collection`` answers 422 and touches
nothing, so the file tier is always an explicit extra tick. (Traktor's middle "strip tags" tier
is not offered: Open DJ writes no tags into audio files, so it had nothing to do.)

* ``remove_from_collection``: the same soft delete as ``POST /tracks/{id}:remove`` (the file
  stays on disk; ``:undelete`` restores the row and its playlist memberships).
* ``delete_file``: the file is MOVED to the user's trash, never unlinked or copied
  (``apps.shared.file_trash``: the macOS Trash API, so Finder's "Put Back" works, also on
  external drives), and the response names the trash path. A rekordbox-mapped track is refused
  (409): its file is still rekordbox's. The move runs inside the remove's transaction, so a
  move the trash refuses answers 409 ``TRACK_TRASH_REFUSED`` and leaves the row live.

``confirm: true`` is required (422 ``CONFIRM_REQUIRED`` otherwise), as on relocate apply, and
the MCP and assistant rails treat ``POST .../{id}:delete`` as destructive
(``apps.opendj_cli.mcp_safety``). ``If-Match`` is required (428 without it); a stale etag
answers 412 and touches nothing. Separate module, sorted before ``tracks.py``, so it registers
ahead of ``/tracks/{stable_id}``. CLI twin: ``opendj track delete <stable_id> [delete-file] yes``.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from apps.shared.events import publish
from apps.shared.file_trash import Trash, TrashRefused, local_trash_bin, move_to_trash
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.shared.state.writer_tracks import TrackAlreadyRemovedError

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import get_library_state_db, get_write_state
from ..etag import compute_etag, strip_quotes

router = APIRouter(prefix="/tracks", tags=["tracks"])
MOUNT_UNDER_API_PREFIX = True


class TrackDeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remove_from_collection: bool
    delete_file: bool = False
    confirm: bool = Field(description="Must be true. Explicit gate: this removes the track and may trash its file.")


class TrackDeleteOut(BaseModel):
    stable_id: str
    deleted_at: str | None
    playlist_ids: list[str]
    trash_path: str | None


def get_trash_bin() -> Trash:
    """This machine's trash; a dependency so tests inject a fake one."""
    return local_trash_bin()


def _refuse(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _check_tiers(body: TrackDeleteIn) -> None:
    if body.delete_file and not body.remove_from_collection:
        raise _refuse(422, "TRACK_DELETE_TIER_ORDER", "delete_file requires remove_from_collection")
    if not body.remove_from_collection:
        raise _refuse(422, "TRACK_DELETE_NOTHING", "set remove_from_collection to delete anything")
    if not body.confirm:
        raise _refuse(422, "CONFIRM_REQUIRED", "confirm must be true to delete a track")


def _file_to_trash(stable_id: str, file_path: str | None, state_db_path: Path) -> Path:
    if rb_vendor.bulk_rb_meta([stable_id], state_db_path=state_db_path):
        raise _refuse(409, "TRACK_DELETE_REKORDBOX_MAPPED", "the file is in rekordbox's collection; delete it there")
    if not file_path or not os.path.isfile(file_path):
        raise _refuse(409, "TRACK_FILE_MISSING", f"no file at the recorded path {file_path!r}")
    return Path(file_path)


@router.post("/{stable_id}:delete", response_model=TrackDeleteOut, operation_id="delete_track_tiers")
def delete_track(
    stable_id: str,
    body: TrackDeleteIn,
    if_match: str | None = Header(None, alias="If-Match"),
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
    state_db_path: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
    trash: Trash = Depends(get_trash_bin),  # noqa: B008  # FastAPI DI
) -> TrackDeleteOut:
    """Remove a track from the collection and, as an explicit extra tier, move its file to the trash."""
    _check_tiers(body)
    if not if_match:
        raise _refuse(428, "PRECONDITION_REQUIRED", "POST /tracks/{stable_id}:delete requires If-Match")
    track = backend.get_track(stable_id)  # NotFoundError -> 404
    if strip_quotes(compute_etag(track.stable_id, track.updated_at, track.selection_tag)) != strip_quotes(if_match):
        raise _refuse(412, "TRACK_ETAG_STALE", "the track changed since it was read; read it again")
    source = _file_to_trash(stable_id, track.file_path, state_db_path) if body.delete_file else None
    trash_path: Path | None = None
    conn = state_db.open_rw(state_db_path)
    try:
        with StateWriter(conn, actor="webui") as writer, writer.transaction():
            result = writer.remove_from_library(stable_id)
            if source is not None:
                trash_path = move_to_trash(source, trash)
    except TrackAlreadyRemovedError as exc:
        raise _refuse(409, "TRACK_ALREADY_REMOVED", str(exc)) from exc
    except TrashRefused as exc:
        raise _refuse(409, "TRACK_TRASH_REFUSED", f"{exc}; the track and its file are untouched") from exc
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
    playlist_ids = sorted({membership.playlist_id for membership in result.memberships})
    publish("library.changed", {"kind": "tracks", "ids": [stable_id], "playlist_ids": playlist_ids})
    return TrackDeleteOut(
        stable_id=stable_id,
        deleted_at=result.deleted_at,
        playlist_ids=playlist_ids,
        trash_path=str(trash_path) if trash_path is not None else None,
    )
