"""Playlist tree order -- LIBTREE-01..03 (ADR-NEW-playlist-tree-order-overlay).

  * ``GET /api/v1/playlist-tree/order``  -> the stored order
  * ``PUT /api/v1/playlist-tree/order``  {order: [playlist_id, ...]} -> stored order
  * ``GET /api/v1/playlist-tree/pins``   -> pinned playlist ids (LIBM-87, issue #12320)
  * ``PUT /api/v1/playlist-tree/pins/{playlist_id}``    -> pin it (404 when no such playlist)
  * ``DELETE /api/v1/playlist-tree/pins/{playlist_id}`` -> unpin it

The tree reads the result through ``GET /api/v1/playlists`` (``seq``), so this
router only owns the write and a read-back. It lives at ``/playlist-tree``,
not under ``/playlists``, because ``GET /playlists/{playlist_id}`` would
swallow any static sub-path registered after it.

Agents drive the same route the drag does (agent-native parity): reorder
playlists, or sort them, by PUTting the full order.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from apps.shared.events import publish

from .. import playlist_tree_order as tree_order
from .. import playlist_tree_pins as tree_pins
from ..backend import StateBackend
from ..deps import get_library_data_dir, get_read_state, get_write_state

router = APIRouter(prefix="/playlist-tree", tags=["playlists-write"])
MOUNT_UNDER_API_PREFIX = True


class PlaylistTreeOrderIn(BaseModel):
    order: list[str] = Field(
        description=(
            "Playlist ids top to bottom. May be partial: playlists it leaves out "
            "follow it in their previous order. Each id must name a live playlist "
            "and appear once."
        ),
    )


class PlaylistTreeOrderOut(BaseModel):
    order: list[str] = Field(description="Stored order, stale ids included as written.")


def _load(path: Path) -> list[str]:
    try:
        return tree_order.load_order(path)
    except tree_order.PlaylistTreeOrderError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "playlist_tree_order_unreadable", "message": str(exc)},
        ) from exc


@router.get("/order", response_model=PlaylistTreeOrderOut)
def get_playlist_tree_order(
    data_dir: Path = Depends(get_library_data_dir),  # noqa: B008  # FastAPI DI
) -> PlaylistTreeOrderOut:
    return PlaylistTreeOrderOut(order=_load(tree_order.path_for(data_dir)))


@router.put("/order", response_model=PlaylistTreeOrderOut)
def put_playlist_tree_order(
    body: PlaylistTreeOrderIn,
    data_dir: Path = Depends(get_library_data_dir),  # noqa: B008  # FastAPI DI
    # get_write_state: the same library-lock policy as every other write.
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> PlaylistTreeOrderOut:
    live_ids = {pl.playlist_id for pl in backend.list_playlists()}
    try:
        order = tree_order.validate_order(body.order, live_ids)
    except tree_order.PlaylistTreeOrderError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_order", "message": str(exc)},
        ) from exc
    try:
        tree_order.save_order(tree_order.path_for(data_dir), order)
    except OSError as exc:
        # Codex P2 on #6186: a read-only dir, a full disk or a failed replace
        # is a structured error the LIBTREE-01 toast can explain, not a bare 500.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "playlist_tree_order_unwritable",
                "message": f"could not store the playlist order: {exc}",
            },
        ) from exc
    publish("library.changed", {"kind": "playlists", "ids": order})
    return PlaylistTreeOrderOut(order=order)


class PlaylistTreePinsOut(BaseModel):
    pins: list[str] = Field(
        description="Pinned playlist ids in pin order; pins of playlists that no longer exist are left out."
    )


def _load_pins(path: Path) -> list[str]:
    try:
        return tree_pins.load_pins(path)
    except tree_order.PlaylistTreeOrderError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "playlist_tree_pins_unreadable", "message": str(exc)},
        ) from exc


def _save_pins(path: Path, pins: list[str]) -> None:
    try:
        tree_pins.save_pins(path, pins)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "playlist_tree_pins_unwritable", "message": f"could not store the playlist pins: {exc}"},
        ) from exc


def _live_ids(backend: StateBackend) -> set[str]:
    return {pl.playlist_id for pl in backend.list_playlists()}


@router.get("/pins", response_model=PlaylistTreePinsOut)
def get_playlist_tree_pins(
    data_dir: Path = Depends(get_library_data_dir),  # noqa: B008  # FastAPI DI
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> PlaylistTreePinsOut:
    pins = _load_pins(tree_pins.path_for(data_dir))
    return PlaylistTreePinsOut(pins=tree_pins.live_pins(pins, _live_ids(backend)))


@router.put("/pins/{playlist_id}", response_model=PlaylistTreePinsOut)
def pin_playlist(
    playlist_id: str,
    data_dir: Path = Depends(get_library_data_dir),  # noqa: B008  # FastAPI DI
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> PlaylistTreePinsOut:
    live_ids = _live_ids(backend)
    if playlist_id not in live_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "playlist_not_found", "message": f"no playlist {playlist_id!r} to pin"},
        )
    path = tree_pins.path_for(data_dir)
    pins = _load_pins(path)
    if playlist_id not in pins:
        pins.append(playlist_id)
        _save_pins(path, pins)
        publish("library.changed", {"kind": "playlists", "ids": [playlist_id]})
    return PlaylistTreePinsOut(pins=tree_pins.live_pins(pins, live_ids))


@router.delete("/pins/{playlist_id}", response_model=PlaylistTreePinsOut)
def unpin_playlist(
    playlist_id: str,
    data_dir: Path = Depends(get_library_data_dir),  # noqa: B008  # FastAPI DI
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> PlaylistTreePinsOut:
    path = tree_pins.path_for(data_dir)
    pins = _load_pins(path)
    if playlist_id in pins:
        pins.remove(playlist_id)
        _save_pins(path, pins)
        publish("library.changed", {"kind": "playlists", "ids": [playlist_id]})
    return PlaylistTreePinsOut(pins=tree_pins.live_pins(pins, _live_ids(backend)))
