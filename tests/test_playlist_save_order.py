"""LIBUX-8678: "Save this order" writes the displayed order into the playlist.

[if] an explicit order of item_ids is saved [then] the playlist reads back in that order, [else stop].
[if] the save is undone [then] the previous order returns, [else stop].
[if] an agent saves ``by=bpm`` or ``by=key`` [then] the stored order follows that sort, untagged tracks last, [else stop].
[if] the ids are not exactly the live items, or the etag is stale [then] nothing changes, [else stop].
[if] a section divider sits on a track [then] the divider stays on that track after the save, [else stop].
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("LIBUX-8678")

# stable_id -> (bpm, key); t-004 is unanalysed.
TRACKS: dict[str, tuple[float | None, str | None]] = {
    "t-001": (128.0, "8A"),
    "t-002": (122.0, "2B"),
    "t-003": (124.0, "2A"),
    "t-004": (None, None),
}
SAVE = "/api/v1/playlists/{pid}/items:save-order"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        for i, (sid, (bpm, key)) in enumerate(TRACKS.items(), start=1):
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
            for name, value in (("bpm", bpm), ("key", key)):
                if value is not None:
                    writer.set_field(sid, name, value, source="rekordbox", modified_at="2026-11-04T09:00:00Z")
    finally:
        writer.close()
        conn.close()
    return path


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    app = create_app(
        backend=SqliteBackend(db_path),
        state_db_path=str(db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as c:
        yield c
    playlist_write.close_store(app)


def _playlist(client: TestClient) -> tuple[str, str]:
    r = client.post("/api/v1/playlists", json={"name": "Set"})
    assert r.status_code == 201, r.text
    pid = r.json()["playlist_id"]
    r = client.put(
        f"/api/v1/playlists/{pid}/tracks", json={"stable_ids": list(TRACKS)}, headers={"If-Match": r.headers["ETag"]}
    )
    assert r.status_code == 200, r.text
    return pid, r.headers["ETag"]


def _tracks(client: TestClient, pid: str) -> list[dict]:
    r = client.get(f"/api/v1/playlists/{pid}")
    assert r.status_code == 200, r.text
    return r.json()["tracks"]


def _order(client: TestClient, pid: str) -> list[str]:
    return [t["stable_id"] for t in _tracks(client, pid)]


def _save(client: TestClient, pid: str, etag: str, body: dict):
    return client.post(SAVE.format(pid=pid), json=body, headers={"If-Match": etag})


def test_explicit_order_is_stored_and_undo_restores_it(client: TestClient) -> None:
    pid, etag = _playlist(client)
    by_sid = {t["stable_id"]: t["item_id"] for t in _tracks(client, pid)}
    wanted = ["t-003", "t-001", "t-004", "t-002"]
    r = _save(client, pid, etag, {"item_ids": [by_sid[s] for s in wanted]})
    assert r.status_code == 200, r.text
    assert r.headers["ETag"] != etag, "if the etag does not move then clients keep a stale order"
    assert _order(client, pid) == wanted, "if the saved order does not read back then save-order is broken"
    assert client.post("/api/v1/playlist-history/undo").status_code == 200
    assert _order(client, pid) == list(TRACKS), "if undo does not restore the old order then save-order is broken"


@pytest.mark.parametrize(
    ("by", "descending", "expected"),
    [
        ("bpm", False, ["t-002", "t-003", "t-001", "t-004"]),
        ("bpm", True, ["t-001", "t-003", "t-002", "t-004"]),
        ("key", False, ["t-003", "t-002", "t-001", "t-004"]),
    ],
)
def test_agent_sort_by_field_is_stored(client: TestClient, by: str, descending: bool, expected: list[str]) -> None:
    pid, etag = _playlist(client)
    r = _save(client, pid, etag, {"by": by, "descending": descending})
    assert r.status_code == 200, r.text
    assert _order(client, pid) == expected, f"if by={by} does not store that sort then save-order is broken"


def test_saving_the_current_order_is_a_no_op(client: TestClient) -> None:
    pid, etag = _playlist(client)
    ids = [t["item_id"] for t in _tracks(client, pid)]
    r = _save(client, pid, etag, {"item_ids": ids})
    assert r.status_code == 200, r.text
    assert r.headers["ETag"] == etag, "if an unchanged order bumps the etag then save-order is not idempotent"


@pytest.mark.parametrize("trim", ["drop_one", "duplicate", "unknown"])
def test_ids_must_be_exactly_the_live_items(client: TestClient, trim: str) -> None:
    pid, etag = _playlist(client)
    ids = [t["item_id"] for t in _tracks(client, pid)]
    bad = {"drop_one": ids[:-1], "duplicate": [*ids[:-1], ids[0]], "unknown": [*ids[:-1], "nope"]}[trim]
    r = _save(client, pid, etag, {"item_ids": bad})
    assert r.status_code == 422, r.text
    assert _order(client, pid) == list(TRACKS), "if a bad id list changes the order then save-order is broken"


def test_body_needs_exactly_one_of_item_ids_or_by(client: TestClient) -> None:
    pid, etag = _playlist(client)
    assert _save(client, pid, etag, {}).status_code == 422
    assert _save(client, pid, etag, {"by": "bpm", "item_ids": []}).status_code == 422


def test_stale_etag_is_refused(client: TestClient) -> None:
    pid, _etag = _playlist(client)
    r = _save(client, pid, '"stale"', {"by": "bpm"})
    assert r.status_code in (409, 412), r.text
    assert _order(client, pid) == list(TRACKS)
    assert client.post(SAVE.format(pid=pid), json={"by": "bpm"}).status_code == 428


def test_section_divider_stays_on_its_track(client: TestClient) -> None:
    pid, etag = _playlist(client)
    r = client.post(
        "/api/v1/sections",
        json={"owner_kind": "playlist", "owner_id": pid, "title": "Peak", "before_position": 2},
        headers={"X-Undo-Session": "save-order-test"},
    )
    assert r.status_code in (200, 201), r.text
    first_in_section = _order(client, pid)[2]
    etag = client.get(f"/api/v1/playlists/{pid}").headers["ETag"]
    r = _save(client, pid, etag, {"by": "bpm"})
    assert r.status_code == 200, r.text
    rows = client.get(f"/api/v1/playlists/{pid}/sections").json()["rows"]
    order = _order(client, pid)
    assert order[rows[0]["before_position"]] == first_in_section, (
        "if the divider does not follow its track then save-order ignores sections"
    )


def test_a_sort_within_sections_keeps_each_divider_at_its_group_start(client: TestClient) -> None:
    pid, _etag = _playlist(client)
    r = client.post(
        "/api/v1/sections",
        json={"owner_kind": "playlist", "owner_id": pid, "title": "Peak", "before_position": 2},
        headers={"X-Undo-Session": "save-order-test"},
    )
    assert r.status_code in (200, 201), r.text
    tracks = _tracks(client, pid)
    by_sid = {t["stable_id"]: t["item_id"] for t in tracks}
    # Each section reversed: [t-002, t-001] | [t-004, t-003]. Following t-003 alone
    # would put the divider at 3.
    etag = client.get(f"/api/v1/playlists/{pid}").headers["ETag"]
    r = _save(client, pid, etag, {"item_ids": [by_sid[s] for s in ["t-002", "t-001", "t-004", "t-003"]]})
    assert r.status_code == 200, r.text
    rows = client.get(f"/api/v1/playlists/{pid}/sections").json()["rows"]
    assert rows[0]["before_position"] == 2, (
        "if a within-sections save moves the divider then the sections are scattered"
    )
    assert _order(client, pid) == ["t-002", "t-001", "t-004", "t-003"]
