"""``POST /api/v1/tracks/list-totals``: track count and summed duration (LIBUX list footer).

Reads each track's duration in one bulk pass and returns the shared aggregate.
An id that is not in the library is a 404 naming it, never a partial sum.

CLI twin: ``opendj track list-totals <id>...``; MCP: ``tracks_list_totals``.

-Cursor
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from apps.shared.list_totals import ListTotals, summarize_durations_ms

from ..backend import StateBackend
from ..deps import get_read_state

router = APIRouter(prefix="/tracks", tags=["tracks"])
MOUNT_UNDER_API_PREFIX = True


class ListTotalsIn(BaseModel):
    stable_ids: list[str] = Field(min_length=1)


class ListTotalsOut(BaseModel):
    count: int
    duration_ms: int
    unknown: int


@router.post("/list-totals", response_model=ListTotalsOut, operation_id="tracks_list_totals")
def tracks_list_totals(
    body: ListTotalsIn,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> ListTotalsOut:
    tracks = backend.get_tracks_bulk(body.stable_ids, live_only=True)
    missing = [sid for sid in dict.fromkeys(body.stable_ids) if sid not in tracks]
    if missing:
        raise HTTPException(status_code=404, detail={"code": "TRACK_NOT_FOUND", "missing": missing})
    totals = summarize_durations_ms(tracks[sid].duration_ms for sid in body.stable_ids)
    return ListTotalsOut(**totals.to_dict())
