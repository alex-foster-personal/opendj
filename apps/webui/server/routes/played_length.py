"""``POST /api/v1/tracks/played-length``: rough played length of a selection (LIBUX played length).

Reads each track's duration and BPM in one bulk pass and hands them to the
shared estimate (:mod:`apps.shared.played_length`). No stored mix-out point
exists in the library today (only the transition-in cue, AGENT-51), so every
track is estimated by the rule of thumb until one does. An id that is not in
the library is a 404 naming it, never a partial sum.

CLI twin: ``opendj track played-length <id>...``; MCP: ``tracks_played_length``.

-Claude
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from apps.shared.played_length import PlayedLengthInput, estimate_played_length

from ..backend import StateBackend
from ..deps import get_read_state

router = APIRouter(prefix="/tracks", tags=["tracks"])


class PlayedLengthIn(BaseModel):
    stable_ids: list[str] = Field(min_length=1)


class PlayedLengthOut(BaseModel):
    played_s: float
    total_s: float
    from_mix_points: int
    from_rule: int
    unknown: int


@router.post("/played-length", response_model=PlayedLengthOut, operation_id="tracks_played_length")
def tracks_played_length(
    body: PlayedLengthIn,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> PlayedLengthOut:
    tracks = backend.get_tracks_bulk(body.stable_ids, live_only=True)
    missing = [sid for sid in dict.fromkeys(body.stable_ids) if sid not in tracks]
    if missing:
        raise HTTPException(status_code=404, detail={"code": "TRACK_NOT_FOUND", "missing": missing})
    inputs = [
        PlayedLengthInput(
            duration_s=None if tracks[sid].duration_ms is None else tracks[sid].duration_ms / 1000,
            bpm=tracks[sid].bpm,
        )
        for sid in body.stable_ids
    ]
    return PlayedLengthOut(**estimate_played_length(inputs).to_dict())
