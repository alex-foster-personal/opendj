"""Playlist tree order -- LIBTREE-01..03 (ADR-NEW-playlist-tree-order-overlay).

  * ``GET /api/v1/playlist-tree/order``  -> the stored order
  * ``PUT /api/v1/playlist-tree/order``  {order: [playlist_id, ...]} -> stored order

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
from ..backend import StateBackend
from ..deps import get_library_data_dir, get_write_state

router = APIRouter(prefix="/playlist-tree", tags=["playlists-write"])


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
