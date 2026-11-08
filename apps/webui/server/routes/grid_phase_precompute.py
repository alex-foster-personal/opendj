"""Status endpoint for the idle grid-phase precompute worker."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request

from apps.webui.server.grid_phase_precompute import GridPhasePrecompute
from apps.webui.server.routes.armed_service import armed_service

router = APIRouter(prefix="/grid-phase-precompute", tags=["grid-phase-precompute"])
MOUNT_UNDER_API_PREFIX = True
NOT_ARMED_CODE: str = "GRID_PHASE_PRECOMPUTE_NOT_ARMED"


_worker: Callable[[Request], GridPhasePrecompute] = armed_service(
    "grid_phase_precompute",
    code=NOT_ARMED_CODE,
    message="the grid-phase precompute is not armed in this engine process",
)


@router.get("", response_model=None)
def get_status(request: Request) -> dict[str, Any]:
    return _worker(request).status().as_dict()


__all__ = ["NOT_ARMED_CODE", "router"]
