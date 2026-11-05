"""Unit tests for shared engine FastAPI app.state readers."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from apps.engine_core.config import EngineConfig
from apps.shared.engine_app_state import (
    data_dir_from_engine_request,
    jobs_runner_from_engine_request,
    jobs_store_from_engine_request,
    require_app_state,
)


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


def test_require_app_state_returns_mounted_value() -> None:
    app = FastAPI()
    sentinel = object()
    app.state.test_attr = sentinel
    assert (
        require_app_state(
            _request_for_app(app),
            "test_attr",
            missing="test missing sentence",
        )
        is sentinel
    )


def test_require_app_state_raises_with_exact_missing_message() -> None:
    app = FastAPI()
    missing = "custom diagnostic when test_attr is absent"
    with pytest.raises(RuntimeError, match=missing) as exc_info:
        require_app_state(
            _request_for_app(app),
            "test_attr",
            missing=missing,
        )
    assert str(exc_info.value) == missing


def test_jobs_store_from_engine_request_returns_mounted_store() -> None:
    app = FastAPI()
    store = object()
    app.state.jobs_store = store
    assert jobs_store_from_engine_request(_request_for_app(app)) is store


def test_jobs_store_from_engine_request_fails_when_missing() -> None:
    app = FastAPI()
    with pytest.raises(RuntimeError, match=r"app\.state\.jobs_store"):
        jobs_store_from_engine_request(_request_for_app(app))


def test_jobs_runner_from_engine_request_returns_mounted_runner() -> None:
    app = FastAPI()
    runner = object()
    app.state.jobs_runner = runner
    assert jobs_runner_from_engine_request(_request_for_app(app)) is runner


def test_jobs_runner_from_engine_request_fails_when_missing() -> None:
    app = FastAPI()
    with pytest.raises(RuntimeError, match=r"app\.state\.jobs_runner"):
        jobs_runner_from_engine_request(_request_for_app(app))
