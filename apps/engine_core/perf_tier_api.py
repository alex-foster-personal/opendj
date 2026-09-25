"""``GET /api/v1/perf-tier`` -- resolved machine tier and scalers."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from apps.engine_core.host_info import (
    CODE_HOST_INFO_UNAVAILABLE,
    HOST_INFO_STATE_ATTR,
    HostIdentity,
)
from apps.shared.perf_tier import (
    HostFacts,
    HostInfoUnavailable,
    tier_wire,
)

PERF_TIER_PATH: str = "/api/v1/perf-tier"
CODE_PERF_TIER_UNAVAILABLE: str = "perf_tier_unavailable"


class PerfTierHostOut(BaseModel):
    logical_cpus: int
    ram_bytes: int


class PerfTierOut(BaseModel):
    tier: str
    source: str
    auto_tier: str | None = None
    override: str
    min_local_stems_tier: str
    scalers: dict[str, int | str]
    host: PerfTierHostOut | None = None


def add_perf_tier_route(app: FastAPI, *, data_dir: Path) -> None:
    @app.get(
        PERF_TIER_PATH,
        response_model=PerfTierOut,
        tags=["health"],
        name="perf_tier",
        responses={
            status.HTTP_503_SERVICE_UNAVAILABLE: {
                "description": "auto tier cannot resolve without host facts"
            }
        },
    )
    def perf_tier() -> PerfTierOut | JSONResponse:
        identity: HostIdentity = getattr(app.state, HOST_INFO_STATE_ATTR)
        facts: HostFacts | None = None
        host_failure: str | None = identity.failure
        if identity.logical_cpus is not None and identity.ram_bytes is not None:
            facts = HostFacts(
                logical_cpus=identity.logical_cpus,
                ram_bytes=identity.ram_bytes,
            )
        try:
            wire = tier_wire(data_dir, facts=facts, host_failure=host_failure)
        except HostInfoUnavailable as exc:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "error": CODE_PERF_TIER_UNAVAILABLE,
                    "message": str(exc),
                    "details": {"host_error": CODE_HOST_INFO_UNAVAILABLE},
                },
            )
        return PerfTierOut.model_validate(wire)


__all__ = [
    "PERF_TIER_PATH",
    "CODE_PERF_TIER_UNAVAILABLE",
    "PerfTierOut",
    "add_perf_tier_route",
]
