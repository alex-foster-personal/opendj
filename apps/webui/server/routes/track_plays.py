"""Open DJ's own play log over HTTP (PLAYS-01).

``POST /tracks/{stable_id}/plays`` records one play of a track in Open DJ.
The browser's play counter (``src/lib/plays/play-counter.ts``) posts once per
deck load, after the load has been heard for ``PLAY_THRESHOLD_S``; any agent
can post the same body, which keeps the flow drivable without a UI.
``GET`` returns the counts the library shows, split by source, so a reader can
see where a number came from.

The library's ``play_count`` is the rekordbox ``DJPlayCount`` PLUS these rows.
Rekordbox never sees an Open DJ play, so the two never count the same play.

Mounted from ``app_wiring.py`` under the API prefix.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.adapters.rekordbox import config as rb_config
from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.shared.state import play_log
from apps.shared.state.sync_stamp import canonical_now

from ..backend import StateBackend
from ..deps import get_read_state, get_write_state
from ..rb_vendor_pkg.track_rows import bulk_rb_meta

router = APIRouter(prefix="/tracks", tags=["track-plays"])


class TrackPlayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    play_id: str = Field(
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
        description="Client-minted id of this deck load; a retry with the same id is not "
        "counted twice.",
    )
    audible_s: float = Field(
        ge=play_log.PLAY_THRESHOLD_S,
        le=86_400,
        description="Seconds the room heard this load (master-routed, above silence).",
    )
    deck: int | None = Field(default=None, ge=1, le=4)
    duration_ms: int | None = Field(default=None, ge=0)


class TrackPlaysOut(BaseModel):
    stable_id: str
    play_count: int = Field(description="rekordbox_play_count + opendj_play_count")
    rekordbox_play_count: int
    opendj_play_count: int
    opendj_last_played_at: str | None
    recorded: bool | None = Field(
        default=None,
        description="POST only: false when this play_id was already logged.",
    )


def _state_db_path(request: Request) -> Path:
    path = getattr(request.app.state, "state_db_path", None)
    return Path(path) if path is not None else rb_config.STATE_DB


def _require_track(conn: sqlite3.Connection, stable_id: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM tracks WHERE stable_id = ? AND deleted_at IS NULL", (stable_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "TRACK_NOT_FOUND", "message": f"no track {stable_id!r}"},
        )


def _plays_out(request: Request, stable_id: str, recorded: bool | None) -> TrackPlaysOut:
    conn = state_db.open_ro(_state_db_path(request))
    try:
        _require_track(conn, stable_id)
        own = play_log.bulk_own_plays(conn, [stable_id]).get(stable_id)
    finally:
        conn.close()
    meta = bulk_rb_meta([stable_id]).get(stable_id)
    rekordbox = meta.play_count if meta is not None else 0
    opendj = own.count if own is not None else 0
    return TrackPlaysOut(
        stable_id=stable_id,
        play_count=rekordbox + opendj,
        rekordbox_play_count=rekordbox,
        opendj_play_count=opendj,
        opendj_last_played_at=own.last_played_at if own is not None else None,
        recorded=recorded,
    )


@router.get("/{stable_id}/plays", response_model=TrackPlaysOut)
def get_track_plays(
    stable_id: str,
    request: Request,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> TrackPlaysOut:
    return _plays_out(request, stable_id, None)


@router.post("/{stable_id}/plays", response_model=TrackPlaysOut)
def record_track_play(
    stable_id: str,
    body: TrackPlayIn,
    request: Request,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> TrackPlaysOut:
    conn = state_db.open_rw(_state_db_path(request))
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_track(conn, stable_id)
        recorded = play_log.record_play(
            conn,
            stable_id,
            play_id=body.play_id,
            audible_s=body.audible_s,
            deck=body.deck,
            duration_ms=body.duration_ms,
            ts=canonical_now(),
            actor="webui",
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if recorded:
        publish("library.changed", {"kind": "plays", "ids": [stable_id]})
    return _plays_out(request, stable_id, recorded)


__all__ = ["TrackPlayIn", "TrackPlaysOut", "router"]
