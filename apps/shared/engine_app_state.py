"""Read engine FastAPI app.state fields shared across routers."""
from __future__ import annotations

from pathlib import Path

from fastapi import Request


def data_dir_from_engine_request(request: Request) -> Path:
    cfg = getattr(request.app.state, "engine_cfg", None)
    if cfg is None:
        raise RuntimeError(
            "engine config is not mounted on app.state.engine_cfg; setup "
            "cannot guess which data dir it is being asked about"
        )
    return Path(cfg.data_dir)


__all__ = ["data_dir_from_engine_request"]
