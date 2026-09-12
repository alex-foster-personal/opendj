"""Live-eval count query tests for GET /smartlists?include_counts=true.

Split from test_smartlists_route.py to stay under the per-file Python line cap.
Shares the same fixture DB pattern as the router tests.

Regression one-liners:
  - if include_counts omitted then count is null
  - if include_counts=true then count matches /tracks items length
  - if track_fields mutate then count re-evaluates on next GET
  - if one rule is corrupt then that row's count is null and list still 200s
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from apps.shared.pairings.schema_sql import ensure_phase08_tables
from apps.shared.state import db as state_db
from apps.smartlists.repo import SmartlistsRepo
from apps.webui.server.app import create_app
from apps.webui.server.routes import smartlists as smartlists_routes
from apps.webui.server.sqlite_backend import SqliteBackend


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


_BASE = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

_SEED_TRACKS: list[tuple[str, str, float, str, int]] = [
    ("sl-track-001", "Midnight Drive", 124.0, "deep house", 4),
    ("sl-track-002", "Oxide", 128.0, "techno", 3),
    ("sl-track-003", "Gulf", 118.0, "ambient", 5),
    ("sl-track-004", "Phoenix", 140.0, "trance", 2),
]

_BPM_RULE: dict = {
    "op": "and",
    "children": [
        {"field": "bpm", "op": ">=", "value": 120},
        {"field": "bpm", "op": "<=", "value": 130},
    ],
}


def _seed_state_db(path: Path) -> None:
    conn = state_db.open_rw(path, apply_schema=True)
    try:
        ensure_phase08_tables(conn)
        for i, (sid, title, bpm, genre, rating) in enumerate(_SEED_TRACKS):
            created = _iso(_BASE.replace(day=1 + i))
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, title, "
                "artists_json, album, isrc, duration_ms, file_path, "
                "content_hash, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    "inferred",
                    title,
                    json.dumps(["Test Artist"]),
                    None,
                    None,
                    300000,
                    None,
                    None,
                    created,
                    created,
                ),
            )
            for fname, value in (
                ("bpm", bpm),
                ("genre", genre),
                ("rating", rating),
            ):
                conn.execute(
                    "INSERT INTO track_fields (stable_id, field_name, "
                    "value_json, source, confidence, modified_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (sid, fname, json.dumps(value), "manual", None, created),
                )
    finally:
        conn.close()


@pytest.fixture
def state_db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    _seed_state_db(path)
    return path


def _make_client(db_path: Path) -> TestClient:
    app = create_app(
        backend=SqliteBackend(db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(db_path),
        mount_frontend=False,
    )
    app.include_router(smartlists_routes.router, prefix="/api/v1")
    return TestClient(app)


@pytest.fixture
def client(state_db_path: Path) -> Iterator[TestClient]:
    with _make_client(state_db_path) as c:
        yield c


def _create_smartlist(
    db_path: Path,
    name: str,
    rule: dict,
    *,
    order_by: str = "added_date desc",
) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        sid = SmartlistsRepo(conn).create(name, rule, order_by=order_by).id
        conn.commit()
        return sid
    finally:
        conn.close()


def test_list_smartlists_include_counts_matches_tracks(client, state_db_path):
    sid = _create_smartlist(state_db_path, "120s", _BPM_RULE)

    listed = client.get("/api/v1/smartlists", params={"include_counts": True})
    tracks = client.get(f"/api/v1/smartlists/{sid}/tracks")

    assert listed.status_code == 200
    assert tracks.status_code == 200
    assert listed.json()[0]["count"] == len(tracks.json()["items"])


def test_list_smartlists_count_re_evaluates_after_field_mutation(
    client, state_db_path,
):
    rule = {"field": "rating", "op": ">=", "value": 4}
    sid = _create_smartlist(state_db_path, "four stars", rule)
    before = client.get(
        "/api/v1/smartlists", params={"include_counts": True},
    )
    before_tracks = client.get(f"/api/v1/smartlists/{sid}/tracks")
    assert before.json()[0]["count"] == len(before_tracks.json()["items"]) == 2

    conn = sqlite3.connect(str(state_db_path))
    try:
        conn.execute(
            "UPDATE track_fields SET value_json=? "
            "WHERE stable_id=? AND field_name='rating'",
            (json.dumps(4), "sl-track-002"),
        )
        conn.commit()
    finally:
        conn.close()

    after = client.get(
        "/api/v1/smartlists", params={"include_counts": True},
    )
    after_tracks = client.get(f"/api/v1/smartlists/{sid}/tracks")
    assert after.json()[0]["count"] == len(after_tracks.json()["items"]) == 3


def test_list_smartlists_corrupt_rule_keeps_collection_available(
    client, state_db_path,
):
    good_id = _create_smartlist(state_db_path, "good", _BPM_RULE)
    bad_id = _create_smartlist(state_db_path, "bad", _BPM_RULE)
    conn = sqlite3.connect(str(state_db_path))
    try:
        conn.execute(
            "UPDATE smartlists SET rule=? WHERE id=?",
            (json.dumps({"field": "unknown", "op": "=", "value": 1}), bad_id),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get(
        "/api/v1/smartlists", params={"include_counts": True},
    )

    assert response.status_code == 200
    counts = {row["id"]: row["count"] for row in response.json()}
    assert counts[good_id] == 2
    assert counts[bad_id] is None
