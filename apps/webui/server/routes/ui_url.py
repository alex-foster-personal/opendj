"""GET /api/v1/ui-url: the browser address of the UI this process serves (UIURL-02).

The HTTP twin of Settings > Open in browser and of ``opendj open browser``.
Refuses with 503 ``UI_NOT_SERVED`` rather than inventing an address when this
process serves no UI (an API-only daemon with no vite frontend paired).
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from ..ui_url import NAMED_LOCAL_HOST, resolve_ui_address

router = APIRouter(prefix="/ui-url", tags=["health"])
MOUNT_UNDER_API_PREFIX = True


class UiUrlOut(BaseModel):
    #: Preferred address, ``http://opendj.localhost:<port>/``.
    ui_url: str
    #: Same UI by loopback IP, for a browser that will not resolve the name.
    loopback_url: str
    port: int
    #: ``engine``: this process serves the bundle. ``dev-frontend``: a dev
    #: backend pointing at its paired vite server.
    served_by: Literal["engine", "dev-frontend"]
    named_host: str


@router.get("", response_model=UiUrlOut)
def ui_url(request: Request) -> UiUrlOut:
    address = resolve_ui_address(request.app.state)
    if address is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "UI_NOT_SERVED",
                "message": (
                    "this process serves no UI: no frontend bundle is mounted "
                    "and the dev frontend is not accepting connections"
                ),
            },
        )
    return UiUrlOut(
        ui_url=address.ui_url,
        loopback_url=address.loopback_url,
        port=address.port,
        served_by=address.served_by,
        named_host=NAMED_LOCAL_HOST,
    )
