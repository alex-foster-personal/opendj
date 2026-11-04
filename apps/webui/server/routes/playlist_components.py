"""Playlist component and route HTTP endpoints (SET-6586).

Components are named, ordered runs of tracks inside a playlist, each with a
slot and free tags. Routes are the ways through a playlist's components; a set
saves one. Same verbs as ``python -m apps.shared.playlist_sets component`` and
``routes``.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from apps.shared.events import publish
from apps.shared.playlist_sets import (
    ComponentInUseError,
    borrowing_playlists,
    create_component,
    delete_component,
    list_borrowed_components,
    list_components,
    list_routes,
    load_component,
    update_component,
)
from apps.shared.playlist_sets.components import ROUTE_LIMIT_DEFAULT, Component, slot_rank

from ..backend import StateBackend
from ..deps import get_read_state, get_write_state
from .playlist_sets import _ensure_playlist, _open_conn

router = APIRouter(prefix="/playlists", tags=["playlist-sets"])


class ComponentOut(BaseModel):
    id: int
    playlist_id: str
    name: str
    slot: str
    stable_ids: list[str]
    tags: list[str]
    created_at: str
    updated_at: str
    # True for a component of another playlist that a saved set borrows.
    borrowed: bool = False


class ComponentListOut(BaseModel):
    components: list[ComponentOut]


class ComponentCreateIn(BaseModel):
    name: str = Field(min_length=1)
    slot: str = Field(min_length=1)
    stable_ids: list[str] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class ComponentUpdateIn(BaseModel):
    """Only the fields given change; ``tags: []`` clears every tag."""

    name: str | None = Field(default=None, min_length=1)
    slot: str | None = Field(default=None, min_length=1)
    stable_ids: list[str] | None = Field(default=None, min_length=1)
    tags: list[str] | None = None


class RouteOut(BaseModel):
    parts: list[int]
    stable_ids: list[str]
    set_id: int | None
    repeats: list[str]


class RouteListOut(BaseModel):
    routes: list[RouteOut]
    total: int
    truncated: bool


def _to_out(c: Component, *, borrowed: bool = False) -> ComponentOut:
    return ComponentOut(
        id=c.id,
        playlist_id=c.playlist_id,
        name=c.name,
        slot=c.slot,
        stable_ids=list(c.stable_ids),
        tags=list(c.tags),
        created_at=c.created_at,
        updated_at=c.updated_at,
        borrowed=borrowed,
    )


@contextmanager
def _store_errors() -> Iterator[None]:
    """Map store refusals to HTTP: bad input 422, missing 404, clashes 409."""
    try:
        yield
    except ComponentInUseError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "component_in_use", "message": str(exc), "set_ids": exc.set_ids},
        ) from exc
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail={"error": "conflict"}
        ) from None
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "invalid_request", "message": str(exc)},
        ) from exc


def _owned_component(conn: sqlite3.Connection, playlist_id: str, component_id: int) -> Component:
    """The component, or 404 when it is missing or belongs to another playlist."""
    with _store_errors():
        comp = load_component(conn, component_id)
    if comp.playlist_id != playlist_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_found"})
    return comp


def _publish_changed(conn: sqlite3.Connection, playlist_id: str, component_id: int) -> None:
    """Tell every view that can show this component: its own playlist and any that borrow it."""
    ids = [playlist_id, str(component_id)]
    ids += [pid for pid in borrowing_playlists(conn, component_id) if pid not in ids]
    publish("library.changed", {"kind": "playlist_components", "ids": ids})
    # A saved set resolves through its components, so an open set tab must refresh too.
    publish("library.changed", {"kind": "playlist_sets", "ids": ids})


@router.get(
    "/{playlist_id}/components",
    response_model=ComponentListOut,
    operation_id="list_playlist_components",
)
def list_playlist_components(
    playlist_id: str,
    request: Request,
    backend: Annotated[StateBackend, Depends(get_read_state)],
) -> ComponentListOut:
    _ensure_playlist(backend, playlist_id)
    conn = _open_conn(request)
    try:
        # Slot ladder order, the same rank the store uses for a borrowed slot this playlist
        # has never owned. Within a slot, own components come first, then borrowed.
        listed = [(c, False) for c in list_components(conn, playlist_id)] + [
            (c, True) for c in list_borrowed_components(conn, playlist_id)
        ]
        listed.sort(key=lambda item: (slot_rank(conn, playlist_id, item[0].slot), item[1]))
        return ComponentListOut(components=[_to_out(c, borrowed=b) for c, b in listed])
    finally:
        conn.close()


@router.post(
    "/{playlist_id}/components",
    response_model=ComponentOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_playlist_component",
)
def create_playlist_component(
    playlist_id: str,
    body: ComponentCreateIn,
    request: Request,
    _backend: Annotated[StateBackend, Depends(get_write_state)],
    backend: Annotated[StateBackend, Depends(get_read_state)],
) -> ComponentOut:
    _ensure_playlist(backend, playlist_id)
    conn = _open_conn(request)
    try:
        with _store_errors():
            cid = create_component(
                conn, playlist_id, body.name, body.slot, body.stable_ids, tags=body.tags
            )
        _publish_changed(conn, playlist_id, cid)
        return _to_out(load_component(conn, cid))
    finally:
        conn.close()


@router.patch(
    "/{playlist_id}/components/{component_id}",
    response_model=ComponentOut,
    operation_id="update_playlist_component",
)
def update_playlist_component(
    playlist_id: str,
    component_id: int,
    body: ComponentUpdateIn,
    request: Request,
    _backend: Annotated[StateBackend, Depends(get_write_state)],
    backend: Annotated[StateBackend, Depends(get_read_state)],
) -> ComponentOut:
    _ensure_playlist(backend, playlist_id)
    conn = _open_conn(request)
    try:
        _owned_component(conn, playlist_id, component_id)
        with _store_errors():
            comp = update_component(
                conn,
                component_id,
                name=body.name,
                slot=body.slot,
                stable_ids=body.stable_ids,
                tags=body.tags,
            )
        _publish_changed(conn, playlist_id, component_id)
        return _to_out(comp)
    finally:
        conn.close()


@router.delete(
    "/{playlist_id}/components/{component_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_playlist_component",
)
def delete_playlist_component(
    playlist_id: str,
    component_id: int,
    request: Request,
    _backend: Annotated[StateBackend, Depends(get_write_state)],
    backend: Annotated[StateBackend, Depends(get_read_state)],
) -> Response:
    _ensure_playlist(backend, playlist_id)
    conn = _open_conn(request)
    try:
        _owned_component(conn, playlist_id, component_id)
        with _store_errors():
            delete_component(conn, component_id)
        _publish_changed(conn, playlist_id, component_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    finally:
        conn.close()


@router.get(
    "/{playlist_id}/routes",
    response_model=RouteListOut,
    operation_id="list_playlist_routes",
)
def list_playlist_routes(
    playlist_id: str,
    request: Request,
    backend: Annotated[StateBackend, Depends(get_read_state)],
    limit: Annotated[int, Query(ge=1, le=1000)] = ROUTE_LIMIT_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RouteListOut:
    _ensure_playlist(backend, playlist_id)
    conn = _open_conn(request)
    try:
        with _store_errors():
            listing = list_routes(conn, playlist_id, limit=limit, offset=offset)
        return RouteListOut(
            routes=[
                RouteOut(
                    parts=list(r.parts),
                    stable_ids=list(r.stable_ids),
                    set_id=r.set_id,
                    repeats=list(r.repeats),
                )
                for r in listing.routes
            ],
            total=listing.total,
            truncated=listing.truncated,
        )
    finally:
        conn.close()
