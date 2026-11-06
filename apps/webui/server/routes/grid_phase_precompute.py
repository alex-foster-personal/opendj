"""Status endpoint for the idle grid-phase precompute worker."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from apps.webui.server.grid_phase_precompute import GridPhasePrecompute

router = APIRouter(prefix="/grid-phase-precompute", tags=["grid-phase-precompute"])
NOT_ARMED_CODE: str = "GRID_PHASE_PRECOMPUTE_NOT_ARMED"


def _worker(request: Request) -> GridPhasePrecompute:
    worker = getattr(request.app.state, "grid_phase_precompute", None)
    if worker is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": NOT_ARMED_CODE,
                "message": "the grid-phase precompute is not armed in this engine process",
            },
        )
    return worker


@router.get("", response_model=None)
def get_status(request: Request) -> dict[str, Any]:
    return _worker(request).status().as_dict()


__all__ = ["NOT_ARMED_CODE", "router"]
