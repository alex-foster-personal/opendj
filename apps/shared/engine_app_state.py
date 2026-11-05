"""Read engine FastAPI app.state fields shared across routers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request


def require_app_state(request: Request, attr: str, *, missing: str) -> Any:
    """Return ``request.app.state.<attr>``, or raise ``RuntimeError(missing)``.

    A missing mount is a boot bug. Never return None or invent a default.
    """
    value = getattr(request.app.state, attr, None)
    if value is None:
        raise RuntimeError(missing)
    return value


def jobs_store_from_engine_request(request: Request) -> Any:
    return require_app_state(
        request,
        "jobs_store",
        missing="engine jobs store is not mounted on app.state.jobs_store",
    )


def jobs_runner_from_engine_request(request: Request) -> Any:
    return require_app_state(
        request,
        "jobs_runner",
        missing="engine jobs runner is not mounted on app.state.jobs_runner",
    )


def data_dir_from_engine_request(request: Request) -> Path:
    cfg = require_app_state(
        request,
        "engine_cfg",
        missing=(
            "engine config is not mounted on app.state.engine_cfg; "
            "data dir cannot be resolved without it"
        ),
    )
    return Path(cfg.data_dir)


__all__ = [
    "data_dir_from_engine_request",
    "jobs_runner_from_engine_request",
    "jobs_store_from_engine_request",
    "require_app_state",
]
