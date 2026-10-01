"""GET /api/v1/search route tests (global-fts5-search node).

Reuses the same fixture-db-building approach as test_search_index.py (real
apps.shared.state schema/writer under tmp_path) so the route test exercises
the full hydration path (SqliteBackend + rb_vendor.build_track_rows), not
just the FTS index in isolation. The "state.db absent" 503 path is real in
this sandbox too -- data/ is gitignored, so the daemon's default state_db_path
genuinely does not exist here; that path is asserted directly rather than
skipped.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server import search_index
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.sqlite_backend import SqliteBackend

_MODIFIED_AT = "2026-04-17T10:00:00.000000Z"


def _set_eav_field(
    conn: sqlite3.Connection, stable_id: str, field_name: str, value: object
) -> None:
    # See tests/webui/test_search_index.py::_set_eav_field for why this
    # writes track_fields directly instead of going through StateWriter.
    conn.execute(
        "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
        "confidence, modified_at) VALUES (?, ?, ?, 'manual', NULL, ?)",
        (stable_id, field_name, json.dumps(value), _MODIFIED_AT),
    )


@pytest.fixture
def fixture_db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    bus = FakeEventBus()
    writer = StateWriter(conn, bus=bus, actor="test")
    try:
        writer.upsert_track(
            stable_id="s1",
            stable_id_tier="inferred",
            title="Lantern Orchard",
            artists=["Mira Valen"],
            album=None,
            isrc=None,
            duration_ms=203_000,
            file_path="/music/s1.mp3",
        )
        _set_eav_field(conn, "s1", "genre", "deep-house")
        writer.upsert_track(
            stable_id="s2",
            stable_id_tier="inferred",
            title="Velvet Static",
            artists=["The Lowlands"],
            album=None,
            isrc=None,
            duration_ms=200_000,
            file_path="/music/s2.mp3",
        )
        _set_eav_field(conn, "s2", "tags", ["synthwave"])
    finally:
        writer.close()
        conn.close()
    return path


@pytest.fixture
def search_client(fixture_db: Path) -> Iterator[TestClient]:
    backend = SqliteBackend(fixture_db)
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(fixture_db),
        mount_frontend=False,
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture
def no_state_db_client(tmp_path: Path) -> Iterator[TestClient]:
    missing = tmp_path / "nope" / "state.db"
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(missing),
        mount_frontend=False,
    )
    with TestClient(app) as c:
        yield c


def test_empty_query_returns_empty_results(search_client: TestClient) -> None:
    r = search_client.get("/api/v1/search", params={"q": ""})
    assert r.status_code == 200
    body = r.json()
    assert body == {"query": "", "items": [], "total": 0, "next_offset": None}


def test_production_app_registers_search_contract() -> None:
    """The production app exposes the same global-search API used by the UI."""
    paths = create_app(mount_frontend=False).openapi()["paths"]

    assert "/api/v1/search" in paths


def test_whitespace_query_returns_empty_results(search_client: TestClient) -> None:
    r = search_client.get("/api/v1/search", params={"q": "   "})
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_search_returns_hydrated_track_row_shape(search_client: TestClient) -> None:
    r = search_client.get("/api/v1/search", params={"q": "lantern"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    hit = body["items"][0]
    # Same field set as playlist-detail rows (models.TrackRowOut) plus
    # match_context.
    for field in (
        "stable_id",
        "title",
        "artist",
        "key",
        "bpm",
        "rating",
        "duration_ms",
        "genre",
        "comments",
        "etag",
        "preview_b64",
        "preview_max",
        "file_exists",
        "is_streaming",
        "is_remote",
        "play_count",
        "vocals",
        "stems",
        "lyrics",
        "is_remix",
        "is_radio_edit",
        "match_context",
    ):
        assert field in hit, f"missing field {field}"
    assert hit["stable_id"] == "s1"
    assert hit["title"] == "Lantern Orchard"
    assert hit["artist"] == "Mira Valen"
    assert "lantern" in hit["match_context"].lower()


def test_search_matches_eav_genre_field(search_client: TestClient) -> None:
    r = search_client.get("/api/v1/search", params={"q": "house"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["stable_id"] == "s1"


def test_search_matches_eav_tags_field(search_client: TestClient) -> None:
    r = search_client.get("/api/v1/search", params={"q": "synthwave"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["stable_id"] == "s2"


def test_search_pagination_next_offset(search_client: TestClient) -> None:
    # "l" prefix-matches "lantern" (s1 title) and "lowlands" (s2 artist) --
    # both fixture tracks, so limit=1 leaves exactly one more page.
    r_all = search_client.get("/api/v1/search", params={"q": "l", "limit": 100})
    assert r_all.json()["total"] == 2

    r_page = search_client.get(
        "/api/v1/search", params={"q": "l", "limit": 1, "offset": 0}
    )
    body = r_page.json()
    assert len(body["items"]) == 1
    assert body["next_offset"] == 1

    r_last = search_client.get(
        "/api/v1/search", params={"q": "l", "limit": 1, "offset": 1}
    )
    assert r_last.json()["next_offset"] is None


def test_search_returns_503_when_state_db_missing(
    no_state_db_client: TestClient,
) -> None:
    r = no_state_db_client.get("/api/v1/search", params={"q": "anything"})
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["code"] == "SEARCH_INDEX_UNAVAILABLE"


def test_search_returns_503_when_index_is_locked(
    search_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_locked_index(_: Path) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(search_index, "_open_index", raise_locked_index)

    response = search_client.get("/api/v1/search", params={"q": "lantern"})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SEARCH_INDEX_UNAVAILABLE"


def test_search_limit_bounds_are_enforced(search_client: TestClient) -> None:
    r = search_client.get("/api/v1/search", params={"q": "lantern", "limit": 0})
    assert r.status_code == 422
    r = search_client.get("/api/v1/search", params={"q": "lantern", "limit": 10_000})
    assert r.status_code == 422


def test_search_rejects_nul_byte_query(search_client: TestClient) -> None:
    r = search_client.get("/api/v1/search", params={"q": "\x00"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "SEARCH_QUERY_INVALID"
    assert "message" in detail


def test_search_rejects_embedded_nul_byte_query(search_client: TestClient) -> None:
    r = search_client.get("/api/v1/search", params={"q": "hello\x00world"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "SEARCH_QUERY_INVALID"
    assert "message" in detail


def test_search_malformed_fts_returns_422_not_500(
    search_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        search_index,
        "build_fts_query",
        lambda _q: '"unterminated',
    )

    r = search_client.get("/api/v1/search", params={"q": "anything"})

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "SEARCH_QUERY_INVALID"
    assert "message" in detail


pytestmark = pytest.mark.rb_parity
