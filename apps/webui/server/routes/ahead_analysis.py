"""HTTP surface of the ahead-of-time analysis drain (NATIVE-21).

  GET  /ahead-analysis/coverage   per lane (strip, loudness, waveform,
                                  beatgrid, key): done / missing / failed with
                                  named reasons over the whole library, served
                                  from the drain's last snapshot (computed_at,
                                  coverage_age_s); never waits on a running tick
  POST /ahead-analysis/retry      re-arm failed tracks and unavailable lanes
  POST /ahead-analysis/bump/{id}  move one track to the front of the drain
  PUT  /ahead-analysis/next-up    AutoPlay's next tracks, analyzed (beatgrid,
                                  key) even while a deck plays (NATIVE-5886)

CLI twin: ``python -m apps.webui.ahead_analysis_cli``.

An engine that never armed the drain answers 503 AHEAD_ANALYSIS_NOT_ARMED.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from apps.webui.server.ahead_analysis import AheadDrain
from apps.webui.server.routes.armed_service import armed_service
from apps.webui.server.routes.state import LEASE_HEADER, live_mirror_lease

router = APIRouter(prefix="/ahead-analysis", tags=["ahead-analysis"])
MOUNT_UNDER_API_PREFIX = True

NOT_ARMED_CODE: str = "AHEAD_ANALYSIS_NOT_ARMED"


_drain: Callable[[Request], AheadDrain] = armed_service(
    "ahead_analysis",
    code=NOT_ARMED_CODE,
    message="the ahead-analysis drain is not running here",
)


@router.get("/coverage")
def get_coverage(request: Request) -> dict[str, Any]:
    return _drain(request).coverage()


@router.post("/retry")
def post_retry(request: Request) -> dict[str, Any]:
    drain = _drain(request)
    drain.retry_failed()
    return {"ok": True}


class NextUpIn(BaseModel):
    """AutoPlay's next tracks, in play order; [] clears them (NATIVE-5886)."""

    model_config = ConfigDict(extra="forbid")
    stable_ids: list[str] = Field(max_length=64)


@router.put("/next-up", response_model=None)
async def put_next_up(
    body: NextUpIn,
    request: Request,
    lease_id: str | None = Header(
        default=None,
        alias=LEASE_HEADER,
        description="AGENT-18: the writing page's client id; refused while another page holds the mirror lease.",
    ),
) -> dict[str, Any] | JSONResponse:
    """Name the tracks analyzed WHILE a deck plays; the drain keeps the first few.

    A page names itself with the ui-mirror lease header, and a PUT from a page
    that does not hold the live lease, including one whose lease lapsed with
    nobody else holding it, is refused (409), so a former leader's late request
    cannot overwrite the current leader's chain. The leader renews its lease
    with every mirror publish, and the page retries a refused PUT. ``async`` on
    purpose: the check and the write run on the event loop with no await
    between them, as the mirror PUT that moves the lease does. A PUT without the
    header (the CLI, agents) is applied as before.
    """
    drain = _drain(request)
    if lease_id is not None:
        current = live_mirror_lease(request)
        if current is None or current.holder != lease_id:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "reason": "lease_lapsed" if current is None else "lease_held",
                    "holder": None if current is None else current.holder,
                },
            )
    return {"ok": True, "next_up": drain.set_next_up(body.stable_ids)}


@router.post("/bump/{stable_id}")
def post_bump(stable_id: str, request: Request) -> dict[str, Any]:
    _drain(request).bump(stable_id)
    return {"ok": True, "stable_id": stable_id}
