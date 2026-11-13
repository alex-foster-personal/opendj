"""Tag List routes: one ordered prep list of up to 100 tracks (rekordbox Tag List parity, issue #10028).

- ``GET /taglist`` returns the list in order with its count and cap.
- ``POST /taglist/items {stable_ids}`` appends; ids already listed are ignored, and an add
  that would pass the cap is refused whole with 409 ``TAGLIST_FULL`` naming the cap.
- ``DELETE /taglist/items/{stable_id}`` removes one track; ``DELETE /taglist`` clears it.
- ``POST /taglist:save {name}`` creates a normal playlist with the rows in order through the
  playlist store (same path as ``POST /playlists``) and leaves the Tag List as it was.

Storage and the deleted-track rule live in :mod:`apps.shared.state.taglist`. The browser
rail entry and the row-menu item are a follow-up PR; ``opendj taglist`` drives these routes.

Mounted from ``app_wiring.py`` under the API prefix.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.shared.state import taglist
from apps.shared.state.sync_stamp import canonical_now

from ..backend import StateBackend
from ..deps import get_read_state, get_write_state
from ..playlist_store import PlaylistStore
from .playlist_write import get_playlist_store
from .track_plays import _state_db_path

router = APIRouter(tags=["taglist"])
MOUNT_UNDER_API_PREFIX = True

_ACTOR = "webui"


class TagListItemOut(BaseModel):
    position: int = Field(description="1-based order in the Tag List")
    stable_id: str
    title: str | None


class TagListOut(BaseModel):
    count: int
    cap: int = Field(description="Most tracks the Tag List holds")
    items: list[TagListItemOut]


class TagListAddIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stable_ids: list[str] = Field(min_length=1, max_length=taglist.TAGLIST_CAP)


class TagListAddOut(TagListOut):
    added: list[str] = Field(description="Ids newly appended (already-listed ids are ignored)")


class TagListClearOut(BaseModel):
    removed: int


class TagListSaveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)


class TagListSaveOut(BaseModel):
    playlist_id: str
    name: str
    track_count: int


def _titles(conn: sqlite3.Connection, stable_ids: list[str]) -> dict[str, str | None]:
    """Titles of the live tracks among ``stable_ids``; a soft-deleted track has no entry."""
    if not stable_ids:
        return {}
    placeholders = ",".join("?" * len(stable_ids))
    return {
        str(sid): title
        for sid, title in conn.execute(
            f"SELECT stable_id, title FROM tracks WHERE deleted_at IS NULL AND stable_id IN ({placeholders})",
            stable_ids,
        )
    }


def _list_out(conn: sqlite3.Connection, items: list[str]) -> TagListOut:
    titles = _titles(conn, items)
    return TagListOut(
        count=len(items),
        cap=taglist.TAGLIST_CAP,
        items=[
            TagListItemOut(position=index, stable_id=sid, title=titles.get(sid))
            for index, sid in enumerate(items, start=1)
        ],
    )


def _changed(stable_ids: list[str]) -> None:
    publish("library.changed", {"kind": "taglist", "ids": stable_ids})


@router.get("/taglist", response_model=TagListOut)
def get_taglist(
    request: Request,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> TagListOut:
    """The Tag List in order."""
    conn = state_db.open_ro(_state_db_path(request))
    try:
        return _list_out(conn, taglist.read_taglist(conn))
    finally:
        conn.close()


@router.post("/taglist/items", response_model=TagListAddOut)
def add_taglist_items(
    body: TagListAddIn,
    request: Request,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> TagListAddOut:
    """Append tracks; duplicates ignored, an add past the cap is refused whole (409)."""
    conn = state_db.open_rw(_state_db_path(request))
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = taglist.add_tracks(conn, body.stable_ids, ts=canonical_now(), actor=_ACTOR)
        conn.commit()
        listed = _list_out(conn, list(result.items))
    except taglist.TagListFullError as error:
        conn.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "TAGLIST_FULL", "cap": taglist.TAGLIST_CAP, "message": str(error)},
        ) from error
    except taglist.UnknownTracksError as error:
        conn.rollback()
        raise HTTPException(
            status_code=422,
            detail={"code": "UNKNOWN_TRACKS", "stable_ids": list(error.stable_ids), "message": str(error)},
        ) from error
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if result.added:
        _changed(list(result.added))
    return TagListAddOut(**listed.model_dump(), added=list(result.added))


@router.delete("/taglist/items/{stable_id}", response_model=TagListOut)
def remove_taglist_item(
    stable_id: str,
    request: Request,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> TagListOut:
    """Remove one track from the Tag List; 404 when it is not listed."""
    conn = state_db.open_rw(_state_db_path(request))
    try:
        conn.execute("BEGIN IMMEDIATE")
        removed = taglist.remove_track(conn, stable_id, ts=canonical_now(), actor=_ACTOR)
        conn.commit()
        listed = _list_out(conn, taglist.read_taglist(conn))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if not removed:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_IN_TAGLIST", "message": f"{stable_id} is not in the Tag List"},
        )
    _changed([stable_id])
    return listed


@router.delete("/taglist", response_model=TagListClearOut)
def clear_taglist(
    request: Request,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> TagListClearOut:
    """Empty the Tag List."""
    conn = state_db.open_rw(_state_db_path(request))
    try:
        conn.execute("BEGIN IMMEDIATE")
        removed = taglist.clear_taglist(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if removed:
        _changed([])
    return TagListClearOut(removed=removed)


@router.post("/taglist:save", response_model=TagListSaveOut, status_code=status.HTTP_201_CREATED)
def save_taglist_as_playlist(
    body: TagListSaveIn,
    request: Request,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
    store: PlaylistStore = Depends(get_playlist_store),  # noqa: B008  # FastAPI DI
) -> TagListSaveOut:
    """Create a normal playlist holding the Tag List in order; the Tag List stays as it was."""
    conn = state_db.open_ro(_state_db_path(request))
    try:
        items = taglist.read_taglist(conn)
    finally:
        conn.close()
    if not items:
        raise HTTPException(
            status_code=422,
            detail={"code": "EMPTY_TAGLIST", "message": "the Tag List is empty; add tracks before saving"},
        )
    created = store.create_playlist(body.name)
    row = store.replace_memberships(created.playlist_id, items, expected_etag=created.etag)
    publish("library.changed", {"kind": "playlists", "ids": [row.playlist_id]})
    return TagListSaveOut(playlist_id=row.playlist_id, name=row.name, track_count=len(row.items))


__all__ = [
    "TagListAddIn",
    "TagListAddOut",
    "TagListClearOut",
    "TagListItemOut",
    "TagListOut",
    "TagListSaveIn",
    "TagListSaveOut",
    "router",
]
