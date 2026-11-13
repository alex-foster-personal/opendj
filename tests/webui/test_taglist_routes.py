"""Tag List prep list routes (issue #10028, rekordbox Tag List parity).

[if] the Tag List routes drift from the rekordbox Tag List (ordered, cap 100, save, clear) [then] fail, [else stop].

Regression one-liners:
  - if GET /taglist does not return rows in add order then the prep list is unusable
  - if a duplicate add appends twice then the list holds repeats
  - if the 101st track is accepted, or the 409 does not name the cap, then the cap is broken
  - if DELETE /taglist/items/{sid} or DELETE /taglist leaves rows then remove and clear are broken
  - if save does not make an ordered playlist, or empties the Tag List, then save is broken
  - if the list is lost on restart, or keeps a deleted track, then persistence is broken
  - if the route's title read returns a soft-deleted track then the read ignores tombstones
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import taglist
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.routes import playlist_write
from apps.webui.server.routes import taglist as taglist_routes
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("LIBORG-14510")

TRACK_IDS = [f"t-{n:03d}" for n in range(1, 106)]
URL = "/api/v1/taglist"


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        for i, sid in enumerate(TRACK_IDS, start=1):
            writer.upsert_track(
                stable_id=sid,
                stable_id_tier="inferred",
                title=f"Track {i}",
                artists=[f"Artist {i}"],
                album=None,
                isrc=None,
                duration_ms=180_000 + i,
                file_path=None,
            )
    finally:
        writer.close()
        conn.close()
    return path


def _app(db_path: Path) -> FastAPI:
    return create_app(
        backend=SqliteBackend(db_path),
        state_db_path=str(db_path),
        mount_frontend=False,
        enable_cors=False,
    )


@pytest.fixture
def client(seeded_db: Path) -> Iterator[TestClient]:
    app = _app(seeded_db)
    try:
        yield TestClient(app)
    finally:
        playlist_write.close_store(app)


def _ids(body: dict[str, object]) -> list[str]:
    return [str(row["stable_id"]) for row in body["items"]]  # type: ignore[index, union-attr]


def test_get_returns_rows_in_add_order(client: TestClient) -> None:
    """[if] GET /taglist does not list rows in add order with positions [then] fail, [else stop]."""
    assert client.post(f"{URL}/items", json={"stable_ids": ["t-003", "t-001"]}).status_code == 200
    assert client.post(f"{URL}/items", json={"stable_ids": ["t-002"]}).status_code == 200
    body = client.get(URL).json()
    assert _ids(body) == ["t-003", "t-001", "t-002"]
    assert [row["position"] for row in body["items"]] == [1, 2, 3]
    assert body["items"][0]["title"] == "Track 3"
    assert body["count"] == 3
    assert body["cap"] == taglist.TAGLIST_CAP == 100


def test_add_ignores_duplicates(client: TestClient) -> None:
    """[if] adding an already-listed id appends it again [then] fail, [else stop]."""
    client.post(f"{URL}/items", json={"stable_ids": ["t-001", "t-002"]})
    response = client.post(f"{URL}/items", json={"stable_ids": ["t-002", "t-001", "t-004", "t-004"]})
    assert response.status_code == 200
    body = response.json()
    assert body["added"] == ["t-004"]
    assert _ids(body) == ["t-001", "t-002", "t-004"]


def test_add_past_cap_is_409_naming_cap_and_changes_nothing(client: TestClient) -> None:
    """[if] the 101st track is accepted or the 409 does not name the cap [then] fail, [else stop]."""
    assert client.post(f"{URL}/items", json={"stable_ids": TRACK_IDS[:100]}).status_code == 200
    response = client.post(f"{URL}/items", json={"stable_ids": [TRACK_IDS[100]]})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "TAGLIST_FULL"
    assert detail["cap"] == 100
    assert "100" in detail["message"]
    assert client.get(URL).json()["count"] == 100


def test_add_unknown_track_is_refused(client: TestClient) -> None:
    """[if] an id with no live track is added [then] fail, [else stop]."""
    response = client.post(f"{URL}/items", json={"stable_ids": ["t-001", "nope"]})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "UNKNOWN_TRACKS"
    assert client.get(URL).json()["count"] == 0


def test_delete_one_and_clear_all(client: TestClient) -> None:
    """[if] DELETE one leaves it listed, or DELETE all leaves rows [then] fail, [else stop]."""
    client.post(f"{URL}/items", json={"stable_ids": ["t-001", "t-002", "t-003"]})
    response = client.delete(f"{URL}/items/t-002")
    assert response.status_code == 200
    assert _ids(response.json()) == ["t-001", "t-003"]
    assert client.delete(f"{URL}/items/t-002").status_code == 404
    cleared = client.delete(URL)
    assert cleared.status_code == 200
    assert cleared.json() == {"removed": 2}
    assert client.get(URL).json()["items"] == []


def test_save_creates_ordered_playlist_and_keeps_taglist(client: TestClient) -> None:
    """[if] save does not make an ordered playlist or changes the Tag List [then] fail, [else stop]."""
    client.post(f"{URL}/items", json={"stable_ids": ["t-005", "t-001", "t-003"]})
    response = client.post(f"{URL}:save", json={"name": "Prep Fri"})
    assert response.status_code == 201
    saved = response.json()
    assert saved["name"] == "Prep Fri"
    assert saved["track_count"] == 3
    playlist = client.get(f"/api/v1/playlists/{saved['playlist_id']}").json()
    assert playlist["items"] == ["t-005", "t-001", "t-003"]
    assert _ids(client.get(URL).json()) == ["t-005", "t-001", "t-003"]


def test_save_empty_taglist_is_refused(client: TestClient) -> None:
    """[if] saving an empty Tag List creates a playlist [then] fail, [else stop]."""
    response = client.post(f"{URL}:save", json={"name": "Empty"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "EMPTY_TAGLIST"


def test_survives_restart_and_drops_deleted_track(seeded_db: Path) -> None:
    """[if] the Tag List is lost on restart or keeps a deleted track [then] fail, [else stop]."""
    first = _app(seeded_db)
    try:
        TestClient(first).post(f"{URL}/items", json={"stable_ids": ["t-001", "t-002", "t-003"]})
    finally:
        playlist_write.close_store(first)
    conn = state_db.open_rw(seeded_db)
    try:
        conn.execute("UPDATE tracks SET deleted_at = '2026-11-13T00:00:00Z' WHERE stable_id = 't-002'")
        conn.commit()
    finally:
        conn.close()
    second = _app(seeded_db)
    try:
        body = TestClient(second).get(URL).json()
    finally:
        playlist_write.close_store(second)
    assert _ids(body) == ["t-001", "t-003"]
    assert body["count"] == 2


def test_title_read_drops_soft_deleted_track(seeded_db: Path) -> None:
    """[if] the route's title read lists a soft-deleted track [then] fail, [else stop]."""
    conn = state_db.open_rw(seeded_db)
    try:
        conn.execute("UPDATE tracks SET deleted_at = '2026-11-13T00:00:00Z' WHERE stable_id = 't-002'")
        conn.commit()
        assert taglist_routes._titles(conn, ["t-001", "t-002", "t-003"]) == {
            "t-001": "Track 1",
            "t-003": "Track 3",
        }
    finally:
        conn.close()


def test_appended_is_pure_and_capped() -> None:
    """[if] appended() reorders, repeats, or passes the cap [then] fail, [else stop]."""
    assert taglist.appended(["a"], ["b", "a", "b", "c"]) == ("a", "b", "c")
    assert taglist.appended(["a", "b"], ["a"], cap=2) == ("a", "b")
    with pytest.raises(taglist.TagListFullError):
        taglist.appended(["a", "b"], ["c"], cap=2)
