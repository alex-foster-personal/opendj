"""``GET /api/v1/app-posture`` -- resolved Gig/Prep posture and scalers."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

from apps.shared.app_posture import posture_wire

APP_POSTURE_PATH: str = "/api/v1/app-posture"


class AppPostureScalersOut(BaseModel):
    library_poll_ms: int
    worker_divisor: int
    prefetch_tracks_floor: int | None = None
    prefetch_bytes_floor: int | None = None


class AppPostureOut(BaseModel):
    posture: str
    label: str
    scalers: AppPostureScalersOut


def add_app_posture_route(app: FastAPI, *, data_dir: Path) -> None:
    @app.get(
        APP_POSTURE_PATH,
        response_model=AppPostureOut,
        tags=["health"],
        name="app_posture",
    )
    def app_posture() -> AppPostureOut:
        return AppPostureOut.model_validate(posture_wire(data_dir))


__all__ = [
    "APP_POSTURE_PATH",
    "AppPostureOut",
    "add_app_posture_route",
]
