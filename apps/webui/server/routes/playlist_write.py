"""Playlist WRITE endpoints -- the playlists-router lane contract.

Contract summary (full details + entity shape: ``..playlist_store`` docstring;
downstream nodes add-remove-reorder / move-copy / write-back / undo all build
on these six endpoints and their etag semantics):

  * ``POST   /api/v1/playlists``                 {name}            -> 201 + ETag
  * ``PATCH  /api/v1/playlists/{id}``            {name?}           -> 200 + ETag
  * ``DELETE /api/v1/playlists/{id}``                              -> 204
  * ``POST   /api/v1/playlists/{id}/duplicate``  {name?} optional  -> 201 + ETag
  * ``PUT    /api/v1/playlists/{id}/tracks``     {stable_ids: []}  -> 200 + ETag
  * ``POST   /api/v1/playlists/{id}/tracks/transfer``  {stable_ids, mode, ...} -> 200

ETag / error semantics copied from the tracks PATCH:

  * PATCH / DELETE / PUT require ``If-Match`` (the etag from a prior response
    header or ``PlaylistWriteOut.updated_at``); missing -> 428
    ``precondition_required``; stale -> 409 ``conflict`` with
    ``{current, etag}`` body + fresh ``ETag`` header (rebase and retry).
  * POST create takes no ``If-Match``. POST duplicate accepts an OPTIONAL
    ``If-Match`` validated against the SOURCE playlist.
  * Unknown playlist / stable_id -> 404 ``not_found`` / 422 ``invalid_patch``.
  * Every 2xx (except 204) returns ``PlaylistWriteOut`` + an ``ETag`` header.

``PUT .../tracks`` is the single membership primitive: the request body is the
complete desired ordering, so add / remove / reorder / move / copy are one
idempotent full-replace call (replaying the same list returns the same etag).

Every successful non-noop write also appends a ``playlist.edit`` event with
before/after snapshots. Downstream undo/redo lives at
``/api/v1/playlist-history`` (never ``/playlists/history``: the read router
already owns ``GET /playlists/{id}``).

Writes go through :class:`..playlist_store.PlaylistStore` ->
:class:`apps.shared.state.writer.StateWriter` (events + provenance). The
integrator wires this router into ``app.py``::

    from .routes import playlist_write as playlist_write_routes
    app.include_router(playlist_write_routes.router, prefix=api_prefix)

and SHOULD call ``playlist_write.close_store(app)`` on shutdown. The store is
built lazily from ``app.state.state_db_path`` (or injected by assigning
``app.state.playlist_store`` before first use); if no state.db exists at that
path, write endpoints fail loudly with 503 -- never a silent in-memory
fallback.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from apps.shared.events import publish

from ..backend import StateBackend
from ..deps import get_write_state
from ..errors import precondition_required
from ..playlist_store import PlaylistRow, PlaylistStore

router = APIRouter(prefix="/playlists", tags=["playlists-write"])

_STORE_INIT_LOCK = threading.Lock()


# --- models (write-contract views; read models stay in ..models) ----------

class PlaylistCreateIn(BaseModel):
    name: str = Field(min_length=1, description="Display name (non-empty)")


class PlaylistRenameIn(BaseModel):
    name: Optional[str] = Field(
        None, min_length=1,
        description="New display name; omit for a no-op that returns the "
                    "current row + etag",
    )


class PlaylistDuplicateIn(BaseModel):
    name: Optional[str] = Field(
        None, min_length=1,
        description="Name for the copy; defaults to '<source name> (copy)'",
    )


class MembershipReplaceIn(BaseModel):
    stable_ids: list[str] = Field(
        description="Complete desired membership in position order. "
                    "Duplicates allowed; unknown ids -> 422."
    )


class MembershipTransferIn(BaseModel):
    stable_ids: list[str] = Field(min_length=1)
    mode: Literal["add", "move"]
    source_playlist_id: str | None = None
    source_etag: str | None = None


class MembershipTransferOut(BaseModel):
    dest: PlaylistWriteOut
    source: PlaylistWriteOut | None = None


class PlaylistWriteOut(BaseModel):
    playlist_id: str
    name: str
    vendor: str
    vendor_pl_id: str
    items: list[str]
    track_count: int
    created_at: str
    updated_at: str


# --- store dependency ------------------------------------------------------

def get_playlist_store(request: Request) -> PlaylistStore:
    store: PlaylistStore | None = getattr(
        request.app.state, "playlist_store", None,
    )
    if store is not None:
        return store
    with _STORE_INIT_LOCK:
        store = getattr(request.app.state, "playlist_store", None)
        if store is not None:
            return store
        db_path = Path(
            getattr(request.app.state, "state_db_path", "data/state/state.db")
        )
        try:
            store = PlaylistStore(db_path)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "state_db_missing",
                    "message": str(exc),
                },
            ) from exc
        request.app.state.playlist_store = store
        return store


def close_store(app) -> None:
    """Integrator shutdown hook: close the lazily-built store, if any."""
    store: PlaylistStore | None = getattr(app.state, "playlist_store", None)
    if store is not None:
        store.close()
        app.state.playlist_store = None


def _out(row: PlaylistRow, response: Response) -> PlaylistWriteOut:
    response.headers["ETag"] = row.etag
    return PlaylistWriteOut(
        playlist_id=row.playlist_id, name=row.name, vendor=row.vendor,
        vendor_pl_id=row.vendor_pl_id, items=list(row.items),
        track_count=len(row.items), created_at=row.created_at,
        updated_at=row.updated_at,
    )


# --- endpoints -------------------------------------------------------------

@router.post("", response_model=PlaylistWriteOut,
             status_code=status.HTTP_201_CREATED)
def create_playlist(
    body: PlaylistCreateIn,
    response: Response,
    _backend: StateBackend = Depends(get_write_state),
    store: PlaylistStore = Depends(get_playlist_store),
) -> PlaylistWriteOut:
    row = store.create_playlist(body.name)
    publish("library.changed", {"kind": "playlists", "ids": [row.playlist_id]})
    return _out(row, response)


@router.patch("/{playlist_id}", response_model=PlaylistWriteOut)
def rename_playlist(
    playlist_id: str,
    body: PlaylistRenameIn,
    response: Response,
    if_match: Optional[str] = Header(None, alias="If-Match"),
    _backend: StateBackend = Depends(get_write_state),
    store: PlaylistStore = Depends(get_playlist_store),
):
    if not if_match:
        return precondition_required(
            "PATCH /playlists/{playlist_id} requires If-Match header"
        )
    if body.name is None:
        # Explicit no-op: CAS-verify and echo the current row.
        row = store.verify_etag(playlist_id, if_match)
        return _out(row, response)
    row = store.rename_playlist(playlist_id, body.name, expected_etag=if_match)
    publish("library.changed", {"kind": "playlists", "ids": [playlist_id]})
    return _out(row, response)


@router.delete("/{playlist_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_playlist(
    playlist_id: str,
    if_match: Optional[str] = Header(None, alias="If-Match"),
    _backend: StateBackend = Depends(get_write_state),
    store: PlaylistStore = Depends(get_playlist_store),
):
    if not if_match:
        return precondition_required(
            "DELETE /playlists/{playlist_id} requires If-Match header"
        )
    store.delete_playlist(playlist_id, expected_etag=if_match)
    publish("library.changed", {"kind": "playlists", "ids": [playlist_id]})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{playlist_id}/duplicate", response_model=PlaylistWriteOut,
             status_code=status.HTTP_201_CREATED)
def duplicate_playlist(
    playlist_id: str,
    response: Response,
    body: Optional[PlaylistDuplicateIn] = None,
    if_match: Optional[str] = Header(None, alias="If-Match"),
    _backend: StateBackend = Depends(get_write_state),
    store: PlaylistStore = Depends(get_playlist_store),
) -> PlaylistWriteOut:
    row = store.duplicate_playlist(
        playlist_id,
        name=body.name if body is not None else None,
        expected_etag=if_match,
    )
    publish("library.changed", {"kind": "playlists", "ids": [row.playlist_id]})
    return _out(row, response)


@router.put("/{playlist_id}/tracks", response_model=PlaylistWriteOut)
def replace_playlist_tracks(
    playlist_id: str,
    body: MembershipReplaceIn,
    response: Response,
    if_match: Optional[str] = Header(None, alias="If-Match"),
    _backend: StateBackend = Depends(get_write_state),
    store: PlaylistStore = Depends(get_playlist_store),
):
    if not if_match:
        return precondition_required(
            "PUT /playlists/{playlist_id}/tracks requires If-Match header"
        )
    row = store.replace_memberships(
        playlist_id, body.stable_ids, expected_etag=if_match,
    )
    publish("library.changed", {"kind": "playlists", "ids": [playlist_id]})
    return _out(row, response)


@router.post("/{playlist_id}/tracks/transfer", response_model=MembershipTransferOut)
def transfer_playlist_tracks(
    playlist_id: str,
    body: MembershipTransferIn,
    response: Response,
    if_match: Optional[str] = Header(None, alias="If-Match"),
    _backend: StateBackend = Depends(get_write_state),
    store: PlaylistStore = Depends(get_playlist_store),
) -> MembershipTransferOut:
    if not if_match:
        return precondition_required(
            "POST /playlists/{playlist_id}/tracks/transfer requires If-Match header"
        )
    dest_row, source_row = store.transfer_memberships(
        playlist_id,
        body.stable_ids,
        dest_etag=if_match,
        mode=body.mode,
        source_id=body.source_playlist_id,
        source_etag=body.source_etag,
    )
    changed_ids = [playlist_id]
    if source_row is not None:
        changed_ids.append(source_row.playlist_id)
    publish("library.changed", {"kind": "playlists", "ids": changed_ids})
    dest_out = _out(dest_row, response)
    source_out = _out(source_row, Response()) if source_row is not None else None
    return MembershipTransferOut(dest=dest_out, source=source_out)
