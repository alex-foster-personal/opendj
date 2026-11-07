"""Hand a track to rekordbox or djay Pro from the track detail page (HANDOFF-01..03)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from ..backend import StateBackend
from ..deps import get_read_state
from ..handoff_apps import HANDOFF_APPS, HandoffApp, HandoffError, open_in_app, resolve_target

router = APIRouter(tags=["tracks"])
MOUNT_UNDER_API_PREFIX = True


class HandoffAppOut(BaseModel):
    app: HandoffApp
    label: str
    available: bool
    # Why the button is disabled; None when the app is installed.
    reason: str | None


class HandoffAppsOut(BaseModel):
    apps: list[HandoffAppOut]


@router.get("/handoff/apps", response_model=HandoffAppsOut, operation_id="list_handoff_apps")
def list_handoff_apps() -> HandoffAppsOut:
    """Which hand-off apps this machine can open a track in, and why not when absent."""
    rows = []
    for app in HANDOFF_APPS:
        target = resolve_target(app)
        rows.append(HandoffAppOut(app=app, label=target.label, available=target.available, reason=target.reason))
    return HandoffAppsOut(apps=rows)


@router.post("/tracks/{stable_id}:open-in", status_code=204, operation_id="open_track_in_app")
def open_track_in_app(
    stable_id: str,
    app: HandoffApp = Query(..., description="Target app: rekordbox or djay"),  # noqa: B008
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> Response:
    """Open a track's local file in rekordbox or djay Pro. Writes nothing of ours."""
    track = backend.get_track(stable_id)
    try:
        open_in_app(app, track.file_path)
    except HandoffError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": exc.message}) from exc
    return Response(status_code=204)
