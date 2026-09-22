"""Adversarial hardening round 4 -- concurrency races on the smartlists CAS.

Every prior round in this workstream exercised sequential requests. Real
usage is not sequential: two browser tabs, or a webui client racing a CLI
edit, can genuinely overlap. Per the workstream's verification discipline
(see .claude/rules/verification.md), this round writes a POSITIVE control
proving the existing protections (the DB UNIQUE(name) constraint, the
revision-CAS in ``SmartlistsRepo.update_rule``) actually hold under REAL
thread-level concurrency against the same sqlite file -- not just
sequential simulation, which cannot catch a race the code has no way to
introduce a artificial gap for.

See specs/library-hardening-adversarial-testing.md for the round log.

Regression one-liners:
  - if two concurrent creates with the same name both succeed then broken
  - if two concurrent CAS updates against the same starting revision both
    succeed (or both fail) then broken
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.pairings.schema_sql import ensure_phase08_tables
from apps.shared.state import db as state_db
from apps.smartlists.repo import SmartlistsRepo
from apps.webui.server.app import create_app
from apps.webui.server.routes import smartlists as smartlists_routes
from apps.webui.server.sqlite_backend import SqliteBackend

_BASE_URL = "http://test-host"
_BASE = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def _seed_state_db(path: Path) -> None:
    conn = state_db.open_rw(path, apply_schema=True)
    try:
        ensure_phase08_tables(conn)
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, "
            "artists_json, album, isrc, duration_ms, file_path, "
            "content_hash, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "hard4-track-001", "inferred", "Probe", json.dumps(["Tester"]),
                None, None, 300000, None, None,
                _BASE.isoformat(), _BASE.isoformat(),
            ),
        )
    finally:
        conn.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    _seed_state_db(path)
    return path


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    app = create_app(
        backend=SqliteBackend(db_path), bind_host="127.0.0.1", hostname="test-host",
        state_db_path=str(db_path), mount_frontend=False,
    )
    app.include_router(smartlists_routes.router, prefix="/api/v1")
    with TestClient(app, base_url=_BASE_URL) as c:
        yield c


def _fire_together(calls: list) -> list:
    """Run ``calls`` (each a zero-arg callable) from separate threads,
    released at the same instant via a barrier so they genuinely overlap
    inside SQLite's locking instead of running one after another."""
    barrier = threading.Barrier(len(calls))

    def _run(fn):
        barrier.wait()
        return fn()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(_run, fn) for fn in calls]
        return [f.result() for f in futures]


def test_concurrent_create_same_name_exactly_one_wins(client, db_path):
    def _create():
        return client.post(
            "/api/v1/smartlists",
            json={"name": "Race", "rule": {"field": "rating", "op": ">=", "value": 0}},
        )

    responses = _fire_together([_create, _create])
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [201, 409], (
        f"expected exactly one 201 and one 409 SMARTLIST_NAME_CONFLICT under a "
        f"real concurrent create race, got {statuses}: "
        f"{[r.text for r in responses]}"
    )
    conflict = next(r for r in responses if r.status_code == 409)
    assert conflict.json()["detail"]["code"] == "SMARTLIST_NAME_CONFLICT"

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM smartlists WHERE name = 'Race'"
        ).fetchone()
    finally:
        conn.close()
    assert rows[0] == 1, "the race must leave exactly one row behind, not zero or two"


def test_concurrent_cas_update_same_starting_revision_exactly_one_wins(client, db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        sid = SmartlistsRepo(conn).create(
            "Editable", {"field": "rating", "op": ">=", "value": 0},
        ).id
        conn.commit()
    finally:
        conn.close()

    etag = client.get(f"/api/v1/smartlists/{sid}").headers["etag"]

    def _update(order_by: str):
        return client.put(
            f"/api/v1/smartlists/{sid}",
            json={
                "rule": {"field": "bpm", "op": ">=", "value": 100},
                "order_by": order_by,
            },
            headers={"If-Match": etag},
        )

    responses = _fire_together([lambda: _update("bpm asc"), lambda: _update("bpm desc")])
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409], (
        f"expected exactly one 200 and one 409 conflict under a real "
        f"concurrent CAS-update race against the same starting revision, "
        f"got {statuses}: {[r.text for r in responses]}"
    )

    winner = next(r for r in responses if r.status_code == 200)
    final = client.get(f"/api/v1/smartlists/{sid}")
    assert final.json()["order_by"] == winner.json()["order_by"], (
        "the persisted row must match the winning writer's value, not a "
        "torn or overwritten mix of both concurrent writes"
    )
