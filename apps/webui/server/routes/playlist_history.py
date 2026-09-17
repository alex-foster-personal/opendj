"""Playlist edit undo/redo endpoints -- playlists-router lane.

Prefix ``/playlist-history`` (not ``/playlists/history``) so the read
router's ``GET /playlists/{playlist_id}`` cannot capture ``history``.

  * ``GET  /api/v1/playlist-history``        live window + cursor
  * ``POST /api/v1/playlist-history/undo``   invert the command at cursor-1
  * ``POST /api/v1/playlist-history/redo``   re-apply the command at cursor

Empty stack is HTTP 409 ``{error: nothing_to_undo|nothing_to_redo}``. Snapshot
mismatch reuses the write-router ConflictError -> 409 ``conflict``. No
If-Match: the before/after snapshot is the CAS truth. 503 when state.db is
missing (same ``get_playlist_store`` as the write router).
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from apps.shared.events import publish

from ..backend import StateBackend
from ..deps import get_write_state
from ..playlist_history import PlaylistEditCommand, PlaylistHistoryEmptyError
from ..playlist_store import PlaylistRow, PlaylistStore
from .playlist_write import PlaylistWriteOut, get_playlist_store

router = APIRouter(prefix="/playlist-history", tags=["playlists-write"])
StateDep = Annotated[StateBackend, Depends(get_write_state)]
StoreDep = Annotated[PlaylistStore, Depends(get_playlist_store)]


class HistoryEntryOut(BaseModel):
    command_id: str
    op: str
    playlist_id: str
    ts: str
    label: str


class HistoryGetOut(BaseModel):
    cursor: int
    limit: int
    can_undo: bool
    can_redo: bool
    entries: list[HistoryEntryOut]


class HistoryApplyOut(BaseModel):
    command_id: str
    op: str
    action: str
    playlist_id: str
    current: PlaylistWriteOut | None = None
    etag: str | None = None
    can_undo: bool
    can_redo: bool


def _row_out(row: PlaylistRow) -> PlaylistWriteOut:
    return PlaylistWriteOut(
        playlist_id=row.playlist_id, name=row.name, vendor=row.vendor,
        vendor_pl_id=row.vendor_pl_id, items=list(row.items),
        track_count=len(row.items), created_at=row.created_at,
        updated_at=row.updated_at, forbid_duplicates=row.forbid_duplicates,
    )


def _empty(action: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"error": f"nothing_to_{action}"},
    )


def _apply_out(
    command: PlaylistEditCommand,
    current: PlaylistRow | None,
    action: str,
    store: PlaylistStore,
) -> JSONResponse:
    hist = store.history()
    payload: dict[str, Any] = {
        "command_id": command.command_id,
        "op": command.op,
        "action": action,
        "playlist_id": command.playlist_id,
        "current": None if current is None else _row_out(current).model_dump(),
        "can_undo": hist["can_undo"],
        "can_redo": hist["can_redo"],
    }
    headers: dict[str, str] = {}
    if current is not None:
        payload["etag"] = current.etag
        headers["ETag"] = current.etag
    publish("library.changed", {"kind": "playlists", "ids": [command.playlist_id]})
    return JSONResponse(content=payload, headers=headers)


@router.get("", response_model=HistoryGetOut)
def get_playlist_history(
    _backend: StateDep,
    store: StoreDep,
) -> HistoryGetOut:
    hist = store.history()
    return HistoryGetOut(
        cursor=hist["cursor"],
        limit=hist["limit"],
        can_undo=hist["can_undo"],
        can_redo=hist["can_redo"],
        entries=[HistoryEntryOut(**entry) for entry in hist["entries"]],
    )


@router.post("/undo", response_model=HistoryApplyOut)
def undo_playlist_edit(
    _backend: StateDep,
    store: StoreDep,
) -> JSONResponse:
    try:
        command, current = store.undo()
    except PlaylistHistoryEmptyError:
        return _empty("undo")
    return _apply_out(command, current, "undo", store)


@router.post("/redo", response_model=HistoryApplyOut)
def redo_playlist_edit(
    _backend: StateDep,
    store: StoreDep,
) -> JSONResponse:
    try:
        command, current = store.redo()
    except PlaylistHistoryEmptyError:
        return _empty("redo")
    return _apply_out(command, current, "redo", store)
