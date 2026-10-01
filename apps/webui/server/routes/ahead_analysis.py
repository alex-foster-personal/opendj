"""HTTP surface of the ahead-of-time analysis drain (NATIVE-21).

  GET  /ahead-analysis/coverage   per lane (strip, loudness, waveform,
                                  beatgrid, key): done / missing / failed with
                                  named reasons over the whole library
  POST /ahead-analysis/retry      re-arm failed tracks and unavailable lanes
  POST /ahead-analysis/bump/{id}  move one track to the front of the drain

CLI twin: ``python -m apps.webui.ahead_analysis_cli``.

An engine that never armed the drain answers 503 AHEAD_ANALYSIS_NOT_ARMED.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from apps.webui.server.ahead_analysis import AheadDrain

router = APIRouter(prefix="/ahead-analysis", tags=["ahead-analysis"])

NOT_ARMED_CODE: str = "AHEAD_ANALYSIS_NOT_ARMED"


def _drain(request: Request) -> AheadDrain:
    drain = getattr(request.app.state, "ahead_analysis", None)
    if drain is None:
        raise HTTPException(
            status_code=503,
            detail={"code": NOT_ARMED_CODE, "message": "the ahead-analysis drain is not running here"},
        )
    return drain


@router.get("/coverage")
def get_coverage(request: Request) -> dict[str, Any]:
    return _drain(request).coverage()


@router.post("/retry")
def post_retry(request: Request) -> dict[str, Any]:
    drain = _drain(request)
    drain.retry_failed()
    return {"ok": True}


@router.post("/bump/{stable_id}")
def post_bump(stable_id: str, request: Request) -> dict[str, Any]:
    _drain(request).bump(stable_id)
    return {"ok": True, "stable_id": stable_id}
