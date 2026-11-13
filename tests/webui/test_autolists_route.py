"""Autolists HTTP router tests (issue #2263 / SMART-06).

[if] autolists groups/buckets/query are called [then] results reflect seeded fields, [else stop].
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.shared.pairings.schema_sql import ensure_phase08_tables
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("SMART-06")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


_BASE = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)

_SEED_TRACKS: list[tuple[str, str, float, str, int]] = [
    ("al-001", "House One", 125.0, "House", 5),
    ("al-002", "Techno Two", 128.0, "Techno", 3),
]


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
                    json.dumps(["Artist"]),
                    None,
                    None,
                    300000,
                    None,
                    None,
                    created,
                    created,
                ),
            )
            for fname, value in (("bpm", bpm), ("genre", genre), ("rating", rating)):
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
    return TestClient(app)


@pytest.fixture
def client(state_db_path: Path) -> Iterator[TestClient]:
    yield _make_client(state_db_path)


def test_groups_200_without_db_fields(client: TestClient) -> None:
    res = client.get("/api/v1/autolists/groups")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 3
    assert {g["id"] for g in data} == {"genre", "rating", "bpm"}


def test_buckets_for_genre(client: TestClient) -> None:
    res = client.get("/api/v1/autolists/buckets", params={"group": "genre"})
    assert res.status_code == 200
    data = res.json()
    labels = {b["label"] for b in data}
    assert "House" in labels
    assert "Techno" in labels


def test_query_empty_selection(client: TestClient) -> None:
    res = client.post(
        "/api/v1/autolists/query",
        json={
            "selection": {"genre": [], "rating": [], "bpm": []},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["items"] == []
    assert body["tracks"] == []
    assert body["total"] == 0


def test_query_house_returns_page(client: TestClient) -> None:
    res = client.post(
        "/api/v1/autolists/query",
        json={
            "selection": {"genre": ["House"], "rating": [], "bpm": []},
            "offset": 0,
            "limit": 500,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["items"] == ["al-001"]
    assert len(body["tracks"]) == 1
    assert body["tracks"][0]["stable_id"] == "al-001"


def test_query_non_numeric_rating_422(client: TestClient) -> None:
    """Issue #2913: non-numeric rating bucket id returns 422, not 500."""
    res = client.post(
        "/api/v1/autolists/query",
        json={
            "limit": 1,
            "offset": 0,
            "selection": {"genre": [], "rating": ["advtest"], "bpm": []},
        },
    )
    assert res.status_code == 422
    body = res.text.lower()
    assert "rating" in body or "selection" in body
    detail = res.json()["detail"]
    assert detail["code"] == "AUTOLIST_SELECTION_INVALID"
    assert detail["field"] == "selection.rating"


def test_query_valid_numeric_rating_200(client: TestClient) -> None:
    """Issue #2913: valid numeric rating bucket id still returns 200."""
    res = client.post(
        "/api/v1/autolists/query",
        json={
            "selection": {"genre": [], "rating": ["4"], "bpm": []},
        },
    )
    assert res.status_code == 200


def test_query_non_numeric_bpm_422(client: TestClient) -> None:
    """Issue #2913: non-numeric bpm bucket id returns 422, not 500."""
    res = client.post(
        "/api/v1/autolists/query",
        json={
            "selection": {"genre": [], "rating": [], "bpm": ["advtest"]},
        },
    )
    assert res.status_code == 422
    body = res.text.lower()
    assert "bpm" in body or "selection" in body
    detail = res.json()["detail"]
    assert detail["code"] == "AUTOLIST_SELECTION_INVALID"
    assert detail["field"] == "selection.bpm"


def _query_selection(client: TestClient, *, genre: list[str], rating: list[str], bpm: list[str]) -> httpx.Response:
    """POST one autolist selection to the query route and return the response."""
    return client.post(
        "/api/v1/autolists/query",
        json={"selection": {"genre": genre, "rating": rating, "bpm": bpm}},
    )


def test_query_valid_rating_bucket_returns_matching_page(client: TestClient) -> None:
    """Issue #13236: a known rating bucket id returns 200 with only its tracks."""
    res = _query_selection(client, genre=[], rating=["5"], bpm=[])
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["items"] == ["al-001"]


def test_query_valid_bpm_bucket_returns_matching_page(client: TestClient) -> None:
    """Issue #13236: a known bpm range bucket id returns 200 with its tracks."""
    res = _query_selection(client, genre=[], rating=[], bpm=["120-129"])
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 2
    assert sorted(body["items"]) == ["al-001", "al-002"]


def test_query_valid_bpm_sentinel_buckets_return_200(client: TestClient) -> None:
    """Issue #13236: the lt60, ge200 and unspecified bpm sentinels are accepted."""
    res = _query_selection(client, genre=[], rating=[], bpm=["lt60", "ge200", "unspecified"])
    assert res.status_code == 200
    assert res.json()["total"] == 0


def test_query_arbitrary_genre_bucket_is_not_rejected(client: TestClient) -> None:
    """Issue #13236: genre ids are free strings, so an unmatched one is 200, not 422."""
    res = _query_selection(client, genre=["advtest"], rating=[], bpm=[])
    assert res.status_code == 200
    assert res.json()["total"] == 0


@pytest.mark.parametrize("bad_id", ["abc-def", "12x-130", "-"])
def test_query_malformed_bpm_range_422(client: TestClient, bad_id: str) -> None:
    """Issue #13236: a bpm range id with non-numeric bounds is a 422 client error."""
    res = _query_selection(client, genre=[], rating=[], bpm=[bad_id])
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert detail["code"] == "AUTOLIST_SELECTION_INVALID"
    assert detail["field"] == "selection.bpm"
    assert bad_id in detail["message"]


@pytest.mark.parametrize("unknown_id", ["0", "6", "9", "-1", "4.5"])
def test_query_unknown_rating_bucket_422(client: TestClient, unknown_id: str) -> None:
    """Issue #13236: a well-formed but unknown rating bucket id is a 422, not a 500."""
    res = _query_selection(client, genre=[], rating=[unknown_id], bpm=[])
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert detail["code"] == "AUTOLIST_SELECTION_INVALID"
    assert detail["field"] == "selection.rating"
    assert unknown_id in detail["message"]


@pytest.mark.parametrize("unknown_id", ["65-74", "200-209", "50-59", "120"])
def test_query_unknown_bpm_bucket_422(client: TestClient, unknown_id: str) -> None:
    """Issue #13236: a well-formed but unknown bpm bucket id is a 422, not a 500."""
    res = _query_selection(client, genre=[], rating=[], bpm=[unknown_id])
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert detail["code"] == "AUTOLIST_SELECTION_INVALID"
    assert detail["field"] == "selection.bpm"
    assert unknown_id in detail["message"]


def test_query_invalid_bucket_among_valid_ones_still_422(client: TestClient) -> None:
    """Issue #13236: one bad id rejects the whole query even beside valid ids."""
    res = _query_selection(client, genre=["House"], rating=["5", "nope"], bpm=["120-129"])
    assert res.status_code == 422
    assert res.json()["detail"]["field"] == "selection.rating"


def test_index_compact_keys(client: TestClient) -> None:
    res = client.get("/api/v1/autolists/index")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 2
    for row in items:
        assert set(row.keys()) == {"stable_id", "genre", "rating", "bpm"}
