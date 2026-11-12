"""Enrollment-tier fixtures for sync_hub lane tests (no cloudsync conftest plugin)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.sync_hub import client as sync_client
from apps.sync_hub import service as sync_service
from tests.cloudsync.enrollment_transport import TestClientTransport

_ENROLL_OWNER_SUB = "google-sub-maintainer"
_ENROLL_OWNER_EMAIL = "maintainer@example.com"


def _seed_user(db_path: Path, *, sub: str, email: str) -> None:
    conn = state_db.open_rw(db_path)
    try:
        stamp = sync_stamp.canonical_now()
        conn.execute(
            "INSERT INTO users(google_sub, email, name, avatar_url, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (sub, email, None, None, stamp, stamp),
        )
    finally:
        conn.close()


def _enroll_hub_app(enroll_hub_dir: Path) -> FastAPI:
    app = FastAPI()
    app.state.state_db_path = str(sync_client.state_db_path(enroll_hub_dir))
    app.state.sync_hub_data_dir = str(enroll_hub_dir)
    app.state.sync_hub_machine_name = "enroll-hub"
    app.include_router(sync_service.router, prefix="/api/v1")
    return app


@pytest.fixture
def clean_machine_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.fixture
def enroll_hub_dir(tmp_path: Path, clean_machine_env: None) -> Path:
    path = tmp_path / "hub"
    path.mkdir(parents=True)
    db_path = sync_client.state_db_path(path)
    state_db.open_rw(db_path).close()
    _seed_user(db_path, sub=_ENROLL_OWNER_SUB, email=_ENROLL_OWNER_EMAIL)
    return path


@pytest.fixture
def enroll_hub(enroll_hub_dir: Path) -> Iterator[TestClientTransport]:
    with TestClient(_enroll_hub_app(enroll_hub_dir)) as http:
        yield TestClientTransport(http)


@pytest.fixture
def enroll_spoke_dir(tmp_path: Path, clean_machine_env: None) -> Path:
    path = tmp_path / "spoke"
    path.mkdir(parents=True)
    return path
