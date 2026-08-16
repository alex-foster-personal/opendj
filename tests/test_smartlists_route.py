"""Smartlists read-router tests (LANE smartlists-router, tree-gear-live-eval).

Self-contained: every functional test builds its OWN tmp state.db (real
Phase 5 migrations + Phase 08 DDL + seeded tracks/track_fields) so no
shared data is touched and nothing here needs the repo's live state.db.
The one live-DB smoke at the bottom skips cleanly when
data/state/state.db is absent.

Regression one-liners:
  - if GET /smartlists does not list a created smartlist then broken
  - if /tracks does not return rule-matching stable_ids in order_by order then broken
  - if /tracks rows are not TrackRowOut-shaped (playlist-detail parity) then broken
  - if unknown id does not 404 SMARTLIST_NOT_FOUND then broken
  - if missing state.db does not 503 SMARTLISTS_DB_UNAVAILABLE then broken
  - if pre-Phase-08 db (no smartlists table) does not list [] then broken
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

# (stable_id, title, bpm, genre, rating)
_SEED_TRACKS: list[tuple[str, str, float, str, int]] = [
    ("sl-track-001", "Midnight Drive", 124.0, "deep house", 4),
    ("sl-track-002", "Oxide", 128.0, "techno", 3),
    ("sl-track-003", "Gulf", 118.0, "ambient", 5),
    ("sl-track-004", "Phoenix", 140.0, "trance", 2),
]


def _seed_state_db(path: Path) -> None:
    """Real Phase 5 schema + Phase 08 tables + 4 tracks with EAV fields."""
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
                    sid, "inferred", title, json.dumps(["Test Artist"]),
                    None, None, 300000, None, None, created, created,
                ),
            )
            for fname, value in (
                ("bpm", bpm), ("genre", genre), ("rating", rating),
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


def _make_client(
    db_path: Path,
    *,
    lock_status_fn=None,
) -> TestClient:
    app = create_app(
        backend=SqliteBackend(db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(db_path),
        mount_frontend=False,
        lock_status_fn=lock_status_fn,
    )
    # app.py is a hotspot owned by the wave integrator; tests wire the
    # router exactly the way the integrator will.
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


_BPM_RULE: dict = {
    "op": "and",
    "children": [
        {"field": "bpm", "op": ">=", "value": 120},
        {"field": "bpm", "op": "<=", "value": 130},
    ],
}


# ----------------------------------------------------------- list + get


def test_list_smartlists_returns_created_rows(client, state_db_path):
    sid = _create_smartlist(state_db_path, "120s", _BPM_RULE)
    r = client.get("/api/v1/smartlists")
    assert r.status_code == 200
    rows = r.json()
    assert [row["id"] for row in rows] == [sid]
    row = rows[0]
    assert row["name"] == "120s"
    assert row["rule"] == _BPM_RULE
    assert "bpm >= 120" in row["rule_summary"]
    assert "AND" in row["rule_summary"]
    assert row["order_by"] == "added_date desc"
    assert row["referenced_fields"] == ["bpm"]


def test_list_smartlists_empty_table(client):
    r = client.get("/api/v1/smartlists")
    assert r.status_code == 200
    assert r.json() == []


def test_list_smartlists_pre_phase08_db(tmp_path):
    # Phase 5 schema only -- no smartlists table. Absence of the lazily
    # created table means zero smartlists, not an error.
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path, apply_schema=True)
    conn.close()
    with _make_client(path) as c:
        r = c.get("/api/v1/smartlists")
    assert r.status_code == 200
    assert r.json() == []


def test_get_smartlist_by_id(client, state_db_path):
    sid = _create_smartlist(state_db_path, "120s", _BPM_RULE)
    r = client.get(f"/api/v1/smartlists/{sid}")
    assert r.status_code == 200
    assert r.json()["name"] == "120s"
    assert r.headers["etag"]


def test_get_smartlist_404(client):
    r = client.get("/api/v1/smartlists/nope")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SMARTLIST_NOT_FOUND"


def test_update_smartlist_returns_persisted_readback(client, state_db_path):
    sid = _create_smartlist(state_db_path, "120s", _BPM_RULE)
    replacement = {"field": "genre", "op": "in", "value": ["drum, bass"]}
    detail = client.get(f"/api/v1/smartlists/{sid}")
    assert detail.status_code == 200
    etag = detail.headers["etag"]

    r = client.put(
        f"/api/v1/smartlists/{sid}",
        json={"rule": replacement, "order_by": "rating desc"},
        headers={"If-Match": etag},
    )

    assert r.status_code == 200
    assert r.json()["id"] == sid
    assert r.json()["rule"] == replacement
    assert r.json()["order_by"] == "rating desc"
    assert r.headers["etag"]
    assert r.headers["etag"] != etag
    readback = client.get(f"/api/v1/smartlists/{sid}")
    assert readback.json()["rule"] == replacement
    assert readback.json()["referenced_fields"] == ["genre"]
    assert readback.headers["etag"] == r.headers["etag"]


def test_update_smartlist_rejects_invalid_rule_explicitly(client, state_db_path):
    sid = _create_smartlist(state_db_path, "120s", _BPM_RULE)
    etag = client.get(f"/api/v1/smartlists/{sid}").headers["etag"]
    r = client.put(
        f"/api/v1/smartlists/{sid}",
        json={"rule": {"field": "mystery", "op": "=", "value": "x"}},
        headers={"If-Match": etag},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "SMARTLIST_RULE_INVALID"


def test_update_smartlist_requires_if_match_without_mutation(client, state_db_path):
    sid = _create_smartlist(state_db_path, "120s", _BPM_RULE)
    before = client.get(f"/api/v1/smartlists/{sid}").json()

    r = client.put(
        f"/api/v1/smartlists/{sid}",
        json={"rule": {"field": "energy", "op": ">=", "value": 7}},
    )

    assert r.status_code == 428
    assert r.json()["error"] == "precondition_required"
    assert client.get(f"/api/v1/smartlists/{sid}").json() == before


def test_update_smartlist_stale_if_match_returns_current_etag_without_mutation(
    client, state_db_path,
):
    sid = _create_smartlist(state_db_path, "120s", _BPM_RULE)
    detail = client.get(f"/api/v1/smartlists/{sid}")
    stale_etag = detail.headers["etag"]
    first_rule = {"field": "energy", "op": ">=", "value": 7}
    first = client.put(
        f"/api/v1/smartlists/{sid}",
        json={"rule": first_rule, "order_by": "energy desc"},
        headers={"If-Match": stale_etag},
    )
    assert first.status_code == 200
    current_etag = first.headers["etag"]

    r = client.put(
        f"/api/v1/smartlists/{sid}",
        json={"rule": {"field": "rating", "op": ">=", "value": 4}},
        headers={"If-Match": stale_etag},
    )

    assert r.status_code == 409
    assert r.headers["etag"] == current_etag
    assert r.json()["error"] == "conflict"
    assert r.json()["current"]["rule"] == first_rule
    persisted = client.get(f"/api/v1/smartlists/{sid}")
    assert persisted.headers["etag"] == current_etag
    assert persisted.json()["rule"] == first_rule
    assert persisted.json()["order_by"] == "energy desc"


def test_update_smartlist_peer_cloud_lock_503_leaves_row_unchanged(
    state_db_path,
):
    sid = _create_smartlist(state_db_path, "120s", _BPM_RULE)
    with _make_client(
        state_db_path,
        lock_status_fn=lambda: {"holder": "other-host"},
    ) as locked_client:
        detail = locked_client.get(f"/api/v1/smartlists/{sid}")
        before = detail.json()
        r = locked_client.put(
            f"/api/v1/smartlists/{sid}",
            json={"rule": {"field": "energy", "op": ">=", "value": 7}},
            headers={"If-Match": detail.headers["etag"]},
        )

    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "locked_by_peer"
    with _make_client(state_db_path) as read_client:
        assert read_client.get(f"/api/v1/smartlists/{sid}").json() == before


def test_smartlists_openapi_documents_cas_headers_and_responses(client):
    schema = client.get("/openapi.json").json()
    detail = schema["paths"]["/api/v1/smartlists/{smartlist_id}"]
    get_operation = detail["get"]
    put_operation = detail["put"]

    assert "ETag" in get_operation["responses"]["200"]["headers"]
    assert any(
        parameter["name"] == "If-Match"
        and parameter["in"] == "header"
        and parameter["required"] is True
        for parameter in put_operation["parameters"]
    )
    assert {"200", "409", "428"} <= set(put_operation["responses"])
    assert "ETag" in put_operation["responses"]["200"]["headers"]
    assert "ETag" in put_operation["responses"]["409"]["headers"]
    assert (
        put_operation["responses"]["409"]["content"]["application/json"]
        ["schema"]["$ref"]
        == "#/components/schemas/SmartlistConflictBody"
    )
    assert (
        put_operation["responses"]["428"]["content"]["application/json"]
        ["schema"]["$ref"]
        == "#/components/schemas/SmartlistPreconditionRequiredBody"
    )
    conflict_schema = schema["components"]["schemas"]["SmartlistConflictBody"]
    assert {"current", "etag"} <= set(conflict_schema["required"])
    assert conflict_schema["properties"]["current"]["$ref"].endswith(
        "/SmartlistSummary"
    )


# ----------------------------------------------------------- evaluation


def test_tracks_evaluates_rule_and_hydrates_rows(client, state_db_path):
    sid = _create_smartlist(state_db_path, "120s", _BPM_RULE)
    r = client.get(f"/api/v1/smartlists/{sid}/tracks")
    assert r.status_code == 200
    body = r.json()
    # bpm 124 + 128 match; added_date desc = insertion days 1/2 reversed.
    assert body["items"] == ["sl-track-002", "sl-track-001"]
    assert body["smartlist_id"] == sid
    assert body["order_by"] == "added_date desc"
    # Hydrated rows: same order as items, playlist-detail TrackRowOut shape.
    assert [t["stable_id"] for t in body["tracks"]] == body["items"]
    row = body["tracks"][0]
    for field in (
        "stable_id", "title", "artist", "key", "bpm", "rating",
        "duration_ms", "genre", "comments", "etag", "preview_b64",
        "preview_max", "file_exists", "is_streaming", "play_count",
    ):
        assert field in row, f"TrackRowOut parity missing {field}"
    assert row["title"] == "Oxide"
    assert row["bpm"] == 128.0
    assert row["file_exists"] is False


def test_tracks_respects_order_by_and_limit(client, state_db_path):
    rule = {"field": "bpm", "op": ">", "value": 0}
    sid = _create_smartlist(
        state_db_path, "all by bpm", rule, order_by="bpm asc",
    )
    r = client.get(f"/api/v1/smartlists/{sid}/tracks")
    assert r.status_code == 200
    assert r.json()["items"] == [
        "sl-track-003", "sl-track-001", "sl-track-002", "sl-track-004",
    ]
    r2 = client.get(f"/api/v1/smartlists/{sid}/tracks", params={"limit": 2})
    assert r2.status_code == 200
    assert r2.json()["items"] == ["sl-track-003", "sl-track-001"]


def test_tracks_contains_predicate(client, state_db_path):
    rule = {"field": "genre", "op": "contains", "value": "techno"}
    sid = _create_smartlist(state_db_path, "techno only", rule)
    r = client.get(f"/api/v1/smartlists/{sid}/tracks")
    assert r.status_code == 200
    assert r.json()["items"] == ["sl-track-002"]


def test_tracks_404_unknown_id(client):
    r = client.get("/api/v1/smartlists/nope/tracks")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SMARTLIST_NOT_FOUND"


# ----------------------------------------------------------- failure modes


def test_missing_state_db_503(tmp_path):
    ghost = tmp_path / "missing" / "state.db"
    app = create_app(
        backend=SqliteBackend(ghost),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(ghost),
        mount_frontend=False,
    )
    app.include_router(smartlists_routes.router, prefix="/api/v1")
    with TestClient(app) as c:
        r = c.get("/api/v1/smartlists")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "SMARTLISTS_DB_UNAVAILABLE"


def test_route_never_writes_state_db(client, state_db_path):
    sid = _create_smartlist(state_db_path, "120s", _BPM_RULE)
    before = state_db_path.read_bytes()
    assert client.get("/api/v1/smartlists").status_code == 200
    assert client.get(f"/api/v1/smartlists/{sid}/tracks").status_code == 200
    assert state_db_path.read_bytes() == before


# ----------------------------------------------------------- live-DB smoke


def test_live_state_db_smoke():
    """Read-only smoke against the repo's real state.db; skips without it."""
    from apps.shared.paths import STATE_DB

    if not Path(STATE_DB).is_file():
        pytest.skip(f"no live state.db at {STATE_DB}")
    app = create_app(
        backend=SqliteBackend(STATE_DB),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(STATE_DB),
        mount_frontend=False,
    )
    app.include_router(smartlists_routes.router, prefix="/api/v1")
    with TestClient(app) as c:
        r = c.get("/api/v1/smartlists")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
