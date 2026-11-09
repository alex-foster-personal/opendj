"""Open DJ's own play log over HTTP (PLAYS-01).

``POST /tracks/{stable_id}/plays`` records one play of a track in Open DJ.
The browser's play counter (``src/lib/plays/play-counter.ts``) posts once per
deck load, after the load has been heard for ``PLAY_THRESHOLD_S``; any agent
can post the same body, which keeps the flow drivable without a UI.
``GET`` returns the counts the library shows, split by source, so a reader can
see where a number came from. ``DELETE`` removes one logged play (Open DJ rows
only; rekordbox ``DJPlayCount`` and imported ``last_played_at`` are untouched).

The library's ``play_count`` is the rekordbox ``DJPlayCount`` PLUS these rows.
Rekordbox never sees an Open DJ play, so the two never count the same play.
Plays imported from Serato history (DJIMPORT-10066) are split out as
``serato_play_count`` / ``serato_last_played_at``.

Mounted from ``app_wiring.py`` under the API prefix.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field

from apps.adapters.rekordbox import config as rb_config
from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.shared.state import play_log
from apps.shared.state.sync_stamp import canonical_now, parse_canonical

from ..backend import StateBackend
from ..deps import get_read_state, get_write_state
from ..rb_vendor_pkg.track_rows import bulk_rb_meta

router = APIRouter(prefix="/tracks", tags=["track-plays"])
MOUNT_UNDER_API_PREFIX = True


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
    play_count: int = Field(
        description="rekordbox_play_count + opendj_play_count + serato_play_count"
    )
    rekordbox_play_count: int
    opendj_play_count: int
    opendj_last_played_at: str | None
    serato_play_count: int
    serato_last_played_at: str | None
    last_played_at: str | None = Field(
        default=None,
        description="Later of the imported Last Played and the latest logged play (library column).",
    )
    recorded: bool | None = Field(
        default=None,
        description="POST only: false when this play_id was already logged.",
    )
    deleted: bool | None = Field(
        default=None,
        description="DELETE only: true when the play row was removed.",
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


def _raise_play_not_found(play_id: str, stable_id: str) -> None:
    raise HTTPException(
        status_code=404,
        detail={
            "code": "PLAY_NOT_FOUND",
            "message": f"no play {play_id!r} for track {stable_id!r}",
        },
    )


def _imported_last_played_at(conn: sqlite3.Connection, stable_id: str) -> str | None:
    row = conn.execute(
        "SELECT value_json FROM track_fields WHERE stable_id = ? AND field_name = 'last_played_at' "
        "ORDER BY updated_at DESC LIMIT 1",
        (stable_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
    if value is None:
        return None
    return str(value) if not isinstance(value, str) else value


def _later_last_played(imported: str | None, opendj: str | None) -> str | None:
    if imported is None:
        return opendj
    if opendj is None:
        return imported
    try:
        imp = parse_canonical(imported)
    except ValueError:
        return opendj
    try:
        own = parse_canonical(opendj)
    except ValueError:
        return imported
    return opendj if own >= imp else imported


def _plays_out(
    request: Request,
    stable_id: str,
    recorded: bool | None,
    *,
    deleted: bool | None = None,
) -> TrackPlaysOut:
    state_path = _state_db_path(request)
    conn = state_db.open_ro(state_path)
    try:
        _require_track(conn, stable_id)
        by_source = play_log.plays_by_source(conn, stable_id)
        imported_lp = _imported_last_played_at(conn, stable_id)
    finally:
        conn.close()
    meta = bulk_rb_meta([stable_id], state_db_path=state_path).get(stable_id)
    rekordbox = meta.play_count if meta is not None else 0
    own = by_source.get(play_log.OPENDJ_SOURCE)
    serato = by_source.get("serato")
    opendj = own.count if own is not None else 0
    serato_count = serato.count if serato is not None else 0
    opendj_last = own.last_played_at if own is not None else None
    serato_last = serato.last_played_at if serato is not None else None
    return TrackPlaysOut(
        stable_id=stable_id,
        play_count=rekordbox + opendj + serato_count,
        rekordbox_play_count=rekordbox,
        opendj_play_count=opendj,
        opendj_last_played_at=opendj_last,
        serato_play_count=serato_count,
        serato_last_played_at=serato_last,
        last_played_at=_later_last_played(
            imported_lp, _later_last_played(serato_last, opendj_last)
        ),
        recorded=recorded,
        deleted=deleted,
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


@router.delete("/{stable_id}/plays/{play_id}", response_model=TrackPlaysOut)
def delete_track_play(
    stable_id: str,
    request: Request,
    play_id: Annotated[
        str,
        ApiPath(
            min_length=8,
            max_length=64,
            pattern=r"^[A-Za-z0-9._-]+$",
        ),
    ],
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> TrackPlaysOut:
    conn = state_db.open_rw(_state_db_path(request))
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_track(conn, stable_id)
        removed = play_log.delete_play(conn, stable_id, play_id)
        if not removed:
            conn.rollback()
            _raise_play_not_found(play_id, stable_id)
        conn.commit()
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    publish("library.changed", {"kind": "plays", "ids": [stable_id]})
    return _plays_out(request, stable_id, None, deleted=True)


__all__ = ["TrackPlayIn", "TrackPlaysOut", "_state_db_path", "router"]
