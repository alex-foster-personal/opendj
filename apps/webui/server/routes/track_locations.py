"""Track location endpoints: relink a track to another file on THIS machine.

The relocate router can only rewrite a track's recorded path, and for a
track with a rekordbox mapping that path lives in the gated live master.db.
Playback never reads that path: it resolves through this machine's
``track_locations`` rows first (``locations.local_audio_path``), so a
relinked alternate row is what makes a track with a dead primary playable
again, while its stable_id (cues, grid edits, playlist slots) stays put.

``GET    /api/v1/tracks/{stable_id}/locations``
    This machine's live location rows for the track.
``POST   /api/v1/tracks/{stable_id}/locations``
    Attach a local file as an ``alternate`` location. Requires
    ``confirm: true`` and ``If-Match`` (the track ETag). The file must be an
    absolute, materialised, non-symlink regular file inside a configured
    music root (same guard as relocate apply). Re-adding a path whose row was
    removed revives that row instead of minting a second one.
``DELETE /api/v1/tracks/{stable_id}/locations/{location_id}``
    The undo: soft-deletes the row (a synced tombstone). Audio files are
    never touched by either write.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.shared.state import locations as state_locations
from apps.shared.state import sync_stamp
from apps.shared.state.writer import StateWriter

from ..backend import StateBackend
from ..deps import get_read_state, get_write_state
from ..etag import require_current_etag
from .relocate import CandidateFile, _mutate_with_candidate_guard, _open_candidate_file

router = APIRouter(prefix="/tracks", tags=["track-locations"])
MOUNT_UNDER_API_PREFIX = True


class TrackLocationOut(BaseModel):
    location_id: str
    stable_id: str
    kind: str
    role: str
    file_path: str | None
    available: bool


class TrackLocationListOut(BaseModel):
    stable_id: str
    locations: list[TrackLocationOut]


class TrackLocationAddIn(BaseModel):
    file_path: str = Field(min_length=1, description="Absolute path of the file to attach.")
    confirm: bool = Field(description="Must be true. Explicit gate: this writes state.db.")


# ----- helpers ---------------------------------------------------------------

def _open_state(request: Request) -> sqlite3.Connection:
    db_path = Path(request.app.state.state_db_path)
    if not db_path.is_file():
        raise HTTPException(status_code=503, detail={
            "code": "STATE_DB_MISSING",
            "message": f"state DB not found at {db_path}",
        })
    return state_db.open_rw(db_path)


def _live_locations(conn: sqlite3.Connection, stable_id: str) -> list[TrackLocationOut]:
    machine_id = sync_stamp.local_machine_id(conn)
    rows = conn.execute(
        "SELECT location_id, stable_id, kind, role, file_path, available "
        "FROM track_locations WHERE stable_id = ? AND machine_id = ? "
        "AND deleted_at IS NULL ORDER BY created_at, location_id",
        (stable_id, machine_id),
    ).fetchall()
    return [
        TrackLocationOut(
            location_id=str(r[0]), stable_id=str(r[1]), kind=str(r[2]),
            role=str(r[3]), file_path=r[4], available=bool(r[5]),
        )
        for r in rows
    ]


# ----- routes ----------------------------------------------------------------

@router.get("/{stable_id}/locations", response_model=TrackLocationListOut)
def list_track_locations(
    stable_id: str,
    request: Request,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> TrackLocationListOut:
    backend.get_track(stable_id)  # NotFoundError -> 404
    conn = _open_state(request)
    try:
        return TrackLocationListOut(stable_id=stable_id, locations=_live_locations(conn, stable_id))
    finally:
        conn.close()


@router.post("/{stable_id}/locations", response_model=TrackLocationOut, status_code=201)
def add_track_location(
    stable_id: str,
    body: TrackLocationAddIn,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> TrackLocationOut:
    if not body.confirm:
        raise HTTPException(status_code=422, detail={
            "code": "CONFIRM_REQUIRED",
            "message": "confirm must be true to attach a location",
        })
    track = backend.get_track(stable_id)  # NotFoundError -> 404
    require_current_etag(
        track,
        if_match,
        precondition_message="track location writes require If-Match (the track ETag)",
        conflict_code="TRACK_ETAG_CONFLICT",
        conflict_message="track changed since it was read; refresh and retry",
    )
    candidate, descriptor = _open_candidate_file(body.file_path)
    os.close(descriptor)
    conn = _open_state(request)
    try:
        def _write(checked: CandidateFile, guard: Callable[[], None]) -> str:
            guard()
            with StateWriter(conn, actor="webui") as writer:
                location_id = writer.upsert_track_location(
                    stable_id=stable_id, kind="local", file_path=checked.path,
                    role="alternate", revive=True,
                )
            guard()
            return location_id

        location_id = _mutate_with_candidate_guard(candidate, candidate.identity_token, _write)
        written = [loc for loc in _live_locations(conn, stable_id) if loc.location_id == location_id]
    finally:
        conn.close()
    if len(written) != 1 or not written[0].available:
        raise HTTPException(status_code=500, detail={
            "code": "TRACK_LOCATION_READBACK_FAILED",
            "message": f"location {location_id} did not read back as a live, available row",
        })
    publish("library.changed", {"kind": "tracks", "ids": [stable_id]})
    return written[0]


@router.delete("/{stable_id}/locations/{location_id}", status_code=204)
def remove_track_location(
    stable_id: str,
    location_id: str,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> Response:
    track = backend.get_track(stable_id)  # NotFoundError -> 404
    require_current_etag(
        track,
        if_match,
        precondition_message="track location writes require If-Match (the track ETag)",
        conflict_code="TRACK_ETAG_CONFLICT",
        conflict_message="track changed since it was read; refresh and retry",
    )
    conn = _open_state(request)
    try:
        with StateWriter(conn, actor="webui") as writer:
            writer.remove_track_location(stable_id=stable_id, location_id=location_id)
    except state_locations.LocationError as exc:
        raise HTTPException(status_code=404, detail={
            "code": "TRACK_LOCATION_NOT_FOUND", "message": str(exc),
        }) from exc
    finally:
        conn.close()
    publish("library.changed", {"kind": "tracks", "ids": [stable_id]})
    return Response(status_code=204)


__all__ = ["router"]
