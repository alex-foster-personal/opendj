"""Agent-readable UI mirror state.

The performance page owns the audio engine, so it pushes its rendered state to
this small engine-side store. A missing push is a closed page, never an empty
mirror that could be mistaken for an idle four-deck screen.

Agents compute staleness from ``received_at``; ``meter.age_ms`` is page-relative
and freezes with the page.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from apps.webui.server.headphone_reports import client_id_of, headphone_reports

router = APIRouter(prefix="/state", tags=["agent-state"])


def _received_at() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _mirror_store(request: Request) -> dict[str, Any] | None:
    mirror = getattr(request.app.state, "ui_mirror", None)
    if mirror is None:
        return None
    if not isinstance(mirror, dict):
        raise TypeError("app.state.ui_mirror must be a JSON object")
    return mirror


@router.put("/ui-mirror", status_code=202)
async def publish_ui_mirror(request: Request, body: dict[str, Any]) -> dict[str, bool]:
    """Replace the page's current screen document with strict JSON input."""
    stored = deepcopy(body)
    stored["received_at"] = _received_at()
    request.app.state.ui_mirror = stored
    # CUEOUT-18: the mirror stays last-writer-wins; the headphone state is
    # also kept per reporting client, so one tab cannot overwrite another's.
    mixer = body.get("mixer")
    headphones = mixer.get("headphones") if isinstance(mixer, dict) else None
    if isinstance(headphones, dict):
        headphone_reports(request.app).record(client_id_of(body), headphones)
    return {"accepted": True}


@router.delete("/ui-mirror", status_code=204)
async def close_ui_mirror(
    request: Request,
    client_id: str | None = Header(
        default=None,
        alias="x-opendj-client-id",
        description=(
            "The closing page's client id (CUEOUT-18). Its headphone report is "
            "forgotten; with no id, every client's report is."
        ),
    ),
) -> None:
    """Mark the performance page closed during its unmount lifecycle."""
    request.app.state.ui_mirror = None
    headphone_reports(request.app).forget(client_id)


@router.get("/ui-mirror", response_model=None)
async def get_ui_mirror(request: Request) -> dict[str, Any] | JSONResponse:
    """Return the latest page mirror, or fail explicitly when no page is open."""
    mirror = _mirror_store(request)
    if mirror is None:
        return JSONResponse(status_code=409, content={"client_open": False})
    return deepcopy(mirror)
