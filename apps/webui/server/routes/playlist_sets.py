"""Playlist-set HTTP endpoints (SET-05).

SET-05 performance objects use ``playlist_sets`` tables, not PLAY-01
``play_orders`` and not SET-01 recorded ``sets``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from apps.shared.events import publish
from apps.shared.playlist_sets import (
    create_playlist_set,
    list_playlist_sets,
    load_playlist_set,
    record_run,
)
from apps.shared.playlist_sets.schema import apply_playlist_set_migrations
from apps.shared.state import db as state_db

from ..backend import StateBackend
from ..deps import get_read_state, get_write_state

router = APIRouter(prefix="/playlists", tags=["playlist-sets"])


class PlaylistSetEntryOut(BaseModel):
    stable_id: str
    position: int


class PlaylistSetRunOut(BaseModel):
    id: int
    kind: Literal["practice", "performance"]
    created_at: str


class PlaylistSetOut(BaseModel):
    id: int
    playlist_id: str
    name: str
    play_count: int
    entries: list[PlaylistSetEntryOut]
    created_at: str
    updated_at: str
    runs: list[PlaylistSetRunOut] | None = None


class PlaylistSetListOut(BaseModel):
    sets: list[PlaylistSetOut]


class PlaylistSetCreateIn(BaseModel):
    name: str = Field(min_length=1)
    from_play_order: str | None = None


class PlaylistSetRunIn(BaseModel):
    kind: Literal["practice", "performance"]


class PlaylistSetRunResultOut(BaseModel):
    set_id: int
    kind: Literal["practice", "performance"]
    play_count: int


def _state_db_path(request: Request) -> Path:
    return Path(getattr(request.app.state, "state_db_path", "data/state/state.db"))


def _open_conn(request: Request) -> sqlite3.Connection:
    db_path = _state_db_path(request)
    if not db_path.exists():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "state_db_missing", "message": f"missing {db_path}"},
        )
    conn = state_db.open_rw(db_path)
    apply_playlist_set_migrations(conn)
    return conn


def _to_out(ps, *, include_runs: bool = False) -> PlaylistSetOut:
    runs = None
    if include_runs:
        runs = [
            PlaylistSetRunOut(id=r.id, kind=r.kind, created_at=r.created_at)
            for r in ps.runs
        ]
    return PlaylistSetOut(
        id=ps.id,
        playlist_id=ps.playlist_id,
        name=ps.name,
        play_count=ps.play_count,
        entries=[
            PlaylistSetEntryOut(stable_id=e.stable_id, position=e.position)
            for e in ps.entries
        ],
        created_at=ps.created_at,
        updated_at=ps.updated_at,
        runs=runs,
    )


def _ensure_playlist(backend: StateBackend, playlist_id: str) -> None:
    try:
        backend.get_playlist(playlist_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found"},
        ) from None


def _publish_changed(playlist_id: str, set_id: int) -> None:
    publish(
        "library.changed",
        {"kind": "playlist_sets", "ids": [playlist_id, str(set_id)]},
    )


@router.get(
    "/{playlist_id}/sets",
    response_model=PlaylistSetListOut,
    operation_id="list_playlist_sets",
)
def list_sets(
    playlist_id: str,
    request: Request,
    backend: Annotated[StateBackend, Depends(get_read_state)],
) -> PlaylistSetListOut:
    _ensure_playlist(backend, playlist_id)
    conn = _open_conn(request)
    try:
        sets = list_playlist_sets(conn, playlist_id)
        return PlaylistSetListOut(sets=[_to_out(s) for s in sets])
    finally:
        conn.close()


@router.post(
    "/{playlist_id}/sets",
    response_model=PlaylistSetOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_playlist_set",
)
def create_set(
    playlist_id: str,
    body: PlaylistSetCreateIn,
    request: Request,
    _backend: Annotated[StateBackend, Depends(get_write_state)],
    backend: Annotated[StateBackend, Depends(get_read_state)],
) -> PlaylistSetOut:
    _ensure_playlist(backend, playlist_id)
    conn = _open_conn(request)
    try:
        try:
            set_id = create_playlist_set(
                conn,
                playlist_id,
                body.name,
                from_play_order=body.from_play_order,
            )
            conn.commit()
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "invalid_request", "message": str(exc)},
            ) from exc
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "message": str(exc)},
            ) from exc
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "conflict"},
            ) from None
        ps = load_playlist_set(conn, set_id)
        _publish_changed(playlist_id, set_id)
        return _to_out(ps)
    finally:
        conn.close()


@router.get(
    "/{playlist_id}/sets/{set_id}",
    response_model=PlaylistSetOut,
    operation_id="get_playlist_set",
)
def get_set(
    playlist_id: str,
    set_id: int,
    request: Request,
    backend: Annotated[StateBackend, Depends(get_read_state)],
) -> PlaylistSetOut:
    _ensure_playlist(backend, playlist_id)
    conn = _open_conn(request)
    try:
        try:
            ps = load_playlist_set(conn, set_id, include_runs=True)
        except LookupError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found"},
            ) from None
        if ps.playlist_id != playlist_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found"},
            )
        return _to_out(ps, include_runs=True)
    finally:
        conn.close()


@router.post(
    "/{playlist_id}/sets/{set_id}/runs",
    response_model=PlaylistSetRunResultOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_playlist_set_run",
)
def create_run(
    playlist_id: str,
    set_id: int,
    body: PlaylistSetRunIn,
    request: Request,
    _backend: Annotated[StateBackend, Depends(get_write_state)],
    backend: Annotated[StateBackend, Depends(get_read_state)],
) -> PlaylistSetRunResultOut:
    _ensure_playlist(backend, playlist_id)
    conn = _open_conn(request)
    try:
        try:
            ps = load_playlist_set(conn, set_id)
        except LookupError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found"},
            ) from None
        if ps.playlist_id != playlist_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found"},
            )
        play_count = record_run(conn, set_id, body.kind)
        conn.commit()
        _publish_changed(playlist_id, set_id)
        return PlaylistSetRunResultOut(
            set_id=set_id, kind=body.kind, play_count=play_count
        )
    finally:
        conn.close()
