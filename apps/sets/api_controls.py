"""Read-only recorded-controls routes for recorded sets (SETREC, PR #14729).

:data:`router` is included into :data:`apps.sets.api.router`, so these routes
mount under the same ``/api/sets`` and ``/api/v1/sets`` prefixes:

    GET    /api/sets/{session_id}/controls         -> validated v2 control events
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import paths as sets_paths
from .api_deps import _sets_root, _share_visible_session
from .control_events import ControlEventsCorruptError, read_session_control_events

# No tags here: the parent router in apps.sets.api tags every included route "sets" once.
router = APIRouter()


class ControlEventsResponse(BaseModel):
    """One session's persisted control-event v2 rows, ascending seq."""

    session_id: str
    events: list[dict[str, Any]]


@router.get("/{session_id}/controls", response_model=ControlEventsResponse)
async def api_session_controls(request: Request, session_id: str) -> ControlEventsResponse:
    """Return one session's validated v2 control events in ascending seq order (SETREC)."""
    _share_visible_session(request, session_id)
    session_dir = sets_paths.session_dir(session_id, root=_sets_root(request))
    try:
        events = await run_in_threadpool(read_session_control_events, session_dir)
    except ControlEventsCorruptError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ControlEventsResponse(session_id=session_id, events=[event.to_json() for event in events])


__all__ = ["ControlEventsResponse", "router"]
