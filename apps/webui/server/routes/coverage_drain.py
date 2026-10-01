"""HTTP surface of the coverage auto-drain (agent parity for the setting).

  GET  /coverage-drain/status   what the drain is doing and what is left,
                                incl. analysis stages reported for the farm
  POST /coverage-drain/start    resume after a stop
  POST /coverage-drain/stop     run no further jobs until started
  PUT  /coverage-drain/config   the persisted setting; any of {"enabled":
                                bool, "steps": {"vocals"|"lyrics"|"analysis":
                                bool}, "transient_bundle_cap": int}. Omitted
                                keys keep their stored value
  POST /coverage-drain/retry    re-arm tracks whose jobs failed terminally

CLI twin: ``python -m apps.webui.coverage_drain_cli``.

An engine that never armed the drain answers 503 COVERAGE_DRAIN_NOT_ARMED:
"not running here" is not "idle", and must not read as it.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from apps.webui.server.coverage_drain import CoverageDrain

router = APIRouter(prefix="/coverage-drain", tags=["coverage-drain"])

NOT_ARMED_CODE: str = "COVERAGE_DRAIN_NOT_ARMED"


class DrainStatusOut(BaseModel):
    state: str
    enabled: bool
    steps_enabled: dict[str, bool]
    stop_requested: bool
    reason: str | None
    pending: dict[str, int]
    failed: dict[str, int]
    waiting_on_stems: int
    stems_needing_farm: list[str]
    stems_needing_farm_count: int
    stems_in_cloud: int
    stems_no_source: int
    stems_index_state: str
    stems_index_reason: str | None
    stems_unclassified: int
    cloud_vocals: dict[str, Any]
    stems_check: dict[str, Any]
    memory_pressure: str | None
    unavailable_steps: dict[str, str]
    farm_only_stages: dict[str, str]
    farm_only_pending: dict[str, int]
    next_retry_at: float | None
    last_job: dict[str, Any] | None
    jobs_run: int
    jobs_failed: int
    ticks: int
    updated_at: float | None


class DrainConfigIn(BaseModel):
    """Partial update: an omitted key keeps its stored value."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    steps: dict[str, bool] | None = None
    transient_bundle_cap: int | None = None


class DrainRetryOut(BaseModel):
    rearmed: int
    status: DrainStatusOut


def _drain(request: Request) -> CoverageDrain:
    drain = getattr(request.app.state, "coverage_drain", None)
    if drain is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": NOT_ARMED_CODE,
                "message": "the coverage auto-drain is not armed in this engine process",
            },
        )
    return drain


def _out(drain: CoverageDrain) -> DrainStatusOut:
    return DrainStatusOut(**drain.status().as_dict())


@router.get("/status", response_model=DrainStatusOut)
def drain_status(request: Request) -> DrainStatusOut:
    return _out(_drain(request))


@router.post("/start", response_model=DrainStatusOut)
def drain_start(request: Request) -> DrainStatusOut:
    drain = _drain(request)
    drain.request_start()
    return _out(drain)


@router.post("/stop", response_model=DrainStatusOut)
def drain_stop(request: Request) -> DrainStatusOut:
    drain = _drain(request)
    drain.request_stop()
    return _out(drain)


@router.put("/config", response_model=DrainStatusOut)
def drain_config(request: Request, body: DrainConfigIn) -> DrainStatusOut:
    drain = _drain(request)
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(422, "name at least one of enabled, steps, transient_bundle_cap")
    try:
        drain.update_config(**changes)
    except (TypeError, ValueError) as error:
        raise HTTPException(422, str(error)) from error
    return _out(drain)


@router.post("/retry", response_model=DrainRetryOut)
def drain_retry(request: Request) -> DrainRetryOut:
    drain = _drain(request)
    return DrainRetryOut(rearmed=drain.retry_failed(), status=_out(drain))


__all__ = ["NOT_ARMED_CODE", "router"]
