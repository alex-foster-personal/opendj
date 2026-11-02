"""Unit tests for shared engine FastAPI app.state readers."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from apps.engine_core.config import EngineConfig
from apps.shared.engine_app_state import data_dir_from_engine_request


def _request_for_app(app: FastAPI) -> Request:
    return Request(
        {
            "type": "http",
            "app": app,
            "headers": [],
            "method": "GET",
            "path": "/",
        }
    )


def test_data_dir_from_engine_request_returns_mounted_data_dir(
    tmp_path: Path,
) -> None:
    app = FastAPI()
    app.state.engine_cfg = EngineConfig(data_dir=tmp_path)
    assert data_dir_from_engine_request(_request_for_app(app)) == tmp_path


def test_data_dir_from_engine_request_fails_when_engine_cfg_missing() -> None:
    app = FastAPI()
    with pytest.raises(RuntimeError, match=r"app\.state\.engine_cfg"):
        data_dir_from_engine_request(_request_for_app(app))
