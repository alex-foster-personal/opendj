"""Set target length, skip-in-set and set summary HTTP endpoints (LIBUX-8679).

- ``GET  /api/v1/playlists/{id}/set-summary``: target, skipped ids and the
  summary ``{played_s, target_s, over_s, skipped, clashes}`` from
  :func:`apps.shared.set_target.summarize_set` (the one function the UI mirrors).
- ``PUT  /api/v1/playlists/{id}/set-target``: ``{"target_s": 3600}``, ``null`` clears.
- ``PUT  /api/v1/playlists/{id}/set-skips/{stable_id}``: ``{"skipped": true}``.

The CLI (``opendj set``) and MCP (``set_summary``, ``set_target``,
``set_skip``) call these routes.

-Claude
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from apps.shared.events import publish
from apps.shared.harmonic import key_to_camelot
from apps.shared.set_target import SetEntry, format_readout, summarize_set
from apps.shared.set_target import store as set_store
from apps.shared.state import db as state_db

from ..backend import StateBackend
from ..deps import get_read_state, get_write_state

router = APIRouter(prefix="/playlists", tags=["set-target"])


class KeyClashOut(BaseModel):
    from_id: str
    to_id: str
    from_key: str
    to_key: str
    steps: int
    gross: bool


class SetSummaryOut(BaseModel):
    playlist_id: str
    played_s: float
    target_s: float | None
    over_s: float | None
    skipped: int
    skipped_ids: list[str]
    clashes: list[KeyClashOut]
    readout: str


class SetTargetIn(BaseModel):
    target_s: float | None = Field(description="Target length in seconds; null clears it.", gt=0)


class SetSkipIn(BaseModel):
    skipped: bool


# ----------------------------------------------------------------- helpers


def _open_conn(request: Request) -> sqlite3.Connection:
    db_path = Path(getattr(request.app.state, "state_db_path", "(private path)"))
    if not db_path.exists():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"error": "state_db_missing", "message": f"missing {db_path}"})
    conn = state_db.open_rw(db_path)
    set_store.ensure_schema(conn)
    return conn


def _camelot_or_none(key: str | None) -> str | None:
    if not key:
        return None
    try:
        return str(key_to_camelot(key))
    except ValueError:
        return None


def _playlist_items(backend: StateBackend, playlist_id: str) -> list[str]:
    try:
        return list(backend.get_playlist(playlist_id).items)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_found"}) from None


def _summary(backend: StateBackend, conn: sqlite3.Connection, playlist_id: str) -> SetSummaryOut:
    items = _playlist_items(backend, playlist_id)
    tracks = backend.get_tracks_bulk(items)
    skipped_ids = set_store.get_skipped(conn, playlist_id) & set(items)
    entries = []
    for sid in items:
        track = tracks.get(sid)
        entries.append(SetEntry(
            stable_id=sid,
            duration_s=None if track is None or track.duration_ms is None else track.duration_ms / 1000.0,
            bpm=None if track is None else track.bpm,
            key=None if track is None else _camelot_or_none(track.key),
            skipped=sid in skipped_ids,
        ))
    target_s = set_store.get_target_s(conn, playlist_id)
    summary = summarize_set(entries, target_s)
    return SetSummaryOut(playlist_id=playlist_id, skipped_ids=sorted(skipped_ids),
                         readout=format_readout(summary.played_s, target_s), **summary.to_dict())


def _publish(playlist_id: str) -> None:
    publish("library.changed", {"kind": "set_target", "ids": [playlist_id]})


# ----------------------------------------------------------------- routes


@router.get("/{playlist_id}/set-summary", response_model=SetSummaryOut, operation_id="get_set_summary")
def get_set_summary(
    playlist_id: str, request: Request, backend: Annotated[StateBackend, Depends(get_read_state)]
) -> SetSummaryOut:
    conn = _open_conn(request)
    try:
        return _summary(backend, conn, playlist_id)
    finally:
        conn.close()


@router.put("/{playlist_id}/set-target", response_model=SetSummaryOut, operation_id="put_set_target")
def put_set_target(
    playlist_id: str, body: SetTargetIn, request: Request,
    backend: Annotated[StateBackend, Depends(get_write_state)],
) -> SetSummaryOut:
    _playlist_items(backend, playlist_id)
    conn = _open_conn(request)
    try:
        set_store.set_target_s(conn, playlist_id, body.target_s)
        conn.commit()
        out = _summary(backend, conn, playlist_id)
    finally:
        conn.close()
    _publish(playlist_id)
    return out


@router.put("/{playlist_id}/set-skips/{stable_id}", response_model=SetSummaryOut, operation_id="put_set_skip")
def put_set_skip(
    playlist_id: str, stable_id: str, body: SetSkipIn, request: Request,
    backend: Annotated[StateBackend, Depends(get_write_state)],
) -> SetSummaryOut:
    if stable_id not in _playlist_items(backend, playlist_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "not_in_playlist", "message": f"{stable_id} is not in {playlist_id}"})
    conn = _open_conn(request)
    try:
        set_store.set_skipped(conn, playlist_id, stable_id, body.skipped)
        conn.commit()
        out = _summary(backend, conn, playlist_id)
    finally:
        conn.close()
    _publish(playlist_id)
    return out
