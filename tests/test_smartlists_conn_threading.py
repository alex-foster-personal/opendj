"""Regression: smartlists conn must survive FastAPI's thread hopping.

Found live Wed 22 Jul 2026 (e2e-gating round): GET /smartlists 500'd with
``sqlite3.ProgrammingError: SQLite objects created in a thread can only be
used in that same thread`` - FastAPI runs sync dependencies and sync
endpoints on DIFFERENT threadpool threads, and the dependency-yielded conn
was created with sqlite's default check_same_thread=True.

- if the conn yielded by get_smartlists_conn cannot execute from another
  thread then broken.
- if a burst of GET /smartlists requests yields any 500 then broken.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state.schema import apply_migrations
from apps.webui.server.app import create_app
from apps.webui.server.routes.smartlists import get_smartlists_conn


@pytest.fixture()
def tmp_state_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "state.db"
    import sqlite3

    conn = sqlite3.connect(db_path)
    apply_migrations(conn)
    conn.close()
    return db_path


def _dependency_conn(db_path: Path):
    class _Req:
        class app:
            class state:
                state_db_path = str(db_path)

    return get_smartlists_conn(_Req())  # type: ignore[arg-type]


def test_conn_usable_from_another_thread(tmp_state_db: Path) -> None:
    gen = _dependency_conn(tmp_state_db)
    conn = next(gen)
    errors: list[BaseException] = []

    def _use() -> None:
        try:
            conn.execute("SELECT 1").fetchone()
        except BaseException as exc:  # noqa: BLE001 - assert below
            errors.append(exc)

    t = threading.Thread(target=_use)
    t.start()
    t.join()
    gen.close()
    assert errors == [], f"cross-thread execute failed: {errors!r}"


def test_smartlists_burst_never_500s(tmp_state_db: Path) -> None:
    app: FastAPI = create_app(
        state_db_path=str(tmp_state_db), mount_frontend=False,
    )
    with TestClient(app) as client:
        for _ in range(25):
            r = client.get("/api/v1/smartlists")
            assert r.status_code == 200, r.text
            assert r.json() == []
