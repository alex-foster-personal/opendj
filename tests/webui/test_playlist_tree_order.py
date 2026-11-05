"""LIBTREE-01..03: the user's playlist tree order (drag-reorder, Sort A-Z).

[if] a user reorders the tree [then] GET /playlists returns that order as seq, [else stop].

Playlists the order leaves out follow it, and a bad order is refused.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.events import EventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server import playlist_tree_order as tree_order
from apps.webui.server.app import create_app
from apps.webui.server.inbox_playlist import INBOX_PLAYLIST_ID
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = [pytest.mark.requirement("LIBTREE-01"), pytest.mark.requirement("LIBTREE-02")]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "state.db"
    path.parent.mkdir(parents=True)
    conn = state_db.open_rw(path)
    bus = EventBus()
    try:
        writer = StateWriter(conn, bus=bus, actor="test-seed")
        writer.close()
    finally:
        bus.close()
        conn.close()
    return path


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    app = create_app(
        backend=SqliteBackend(db_path), state_db_path=str(db_path),
        bind_host="127.0.0.1", hostname="test-host", mount_frontend=False,
    )
    with TestClient(app) as c:
        yield c
    playlist_write.close_store(app)


def _create(client: TestClient, name: str) -> str:
    r = client.post("/api/v1/playlists", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["playlist_id"]


def _seqs(client: TestClient) -> dict[str, int | None]:
    r = client.get("/api/v1/playlists", params={"availability": "skip"})
    assert r.status_code == 200, r.text
    return {
        row["name"]: row["seq"]
        for row in r.json()
        if row["playlist_id"] != INBOX_PLAYLIST_ID
    }


def test_no_stored_order_keeps_the_old_seq(client: TestClient, db_path: Path) -> None:
    _create(client, "B")
    _create(client, "A")
    # Control: with no file, own playlists carry no seq (the tree name-sorts them).
    assert _seqs(client) == {"A": None, "B": None}
    assert not tree_order.path_for(db_path.parent.parent).exists()
    assert client.get("/api/v1/playlist-tree/order").json() == {"order": []}


def test_put_order_becomes_seq_and_persists(client: TestClient, db_path: Path) -> None:
    a = _create(client, "A")
    b = _create(client, "B")
    c = _create(client, "C")
    r = client.put("/api/v1/playlist-tree/order", json={"order": [c, a, b]})
    assert r.status_code == 200, r.text
    assert _seqs(client) == {"C": 0, "A": 1, "B": 2}
    stored = tree_order.load_order(tree_order.path_for(db_path.parent.parent))
    assert stored == [c, a, b]
    assert client.get("/api/v1/playlist-tree/order").json() == {"order": [c, a, b]}


def test_saved_order_uses_lf_bytes_on_every_platform(tmp_path: Path) -> None:
    """Codex P2 on #6186: the overlay file is byte-identical across platforms."""
    path = tmp_path / "order.json"
    tree_order.save_order(path, ["a", "b"])
    raw = path.read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"]\n}\n")


def test_non_file_overlay_fails_loud(tmp_path: Path) -> None:
    """Codex P2 on #6186: a directory where the overlay belongs is an error."""
    path = tmp_path / tree_order.FILENAME
    path.mkdir()
    with pytest.raises(tree_order.PlaylistTreeOrderError, match="not a regular file"):
        tree_order.load_order(path)
    assert tree_order.load_order(tmp_path / "missing.json") == []


def test_invalid_utf8_overlay_is_the_contract_error(
    client: TestClient, db_path: Path
) -> None:
    """Codex P2 on #6186: non UTF-8 bytes are unreadable, not a generic 500."""
    path = tree_order.path_for(db_path.parent.parent)
    _create(client, "A")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"order": ["\xff\xfe"]}')
    with pytest.raises(tree_order.PlaylistTreeOrderError, match="unreadable"):
        tree_order.load_order(path)
    for url in ("/api/v1/playlists", "/api/v1/playlist-tree/order"):
        r = client.get(url)
        assert r.status_code == 500, r.text
        assert r.json()["detail"]["error"] == "playlist_tree_order_unreadable"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows reports a file used as a directory as FileNotFoundError (path not found), "
    "which is the 'never reordered' case; ENOTDIR is the POSIX stat failure under test",
)
def test_overlay_stat_failure_is_the_contract_error(tmp_path: Path) -> None:
    """Codex P2 on #6186 (r4266167694): a real stat failure, no monkeypatch.

    A regular file sits where the overlay's parent directory goes, so the
    production stat raises NotADirectoryError (ENOTDIR). The routes map PlaylistTreeOrderError to
    playlist_tree_order_unreadable (see the invalid UTF-8 test); the overlay
    shares its directory with state.db, so the route cannot be driven this way.
    """
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x\n", encoding="utf-8")
    with pytest.raises(tree_order.PlaylistTreeOrderError, match="unreadable"):
        tree_order.load_order(blocker / tree_order.FILENAME)


def test_duplicate_ids_in_the_overlay_fail_loud(client: TestClient, db_path: Path) -> None:
    """Codex P2 on #6186: LIBTREE-03, a stored order naming a playlist twice."""
    a = _create(client, "A")
    path = tree_order.path_for(db_path.parent.parent)
    path.write_text(json.dumps({"order": [a, a]}) + "\n", encoding="utf-8")
    with pytest.raises(tree_order.PlaylistTreeOrderError, match="stored twice"):
        tree_order.load_order(path)
    for url in ("/api/v1/playlists", "/api/v1/playlist-tree/order"):
        r = client.get(url)
        assert r.status_code == 500, r.text
        assert r.json()["detail"]["error"] == "playlist_tree_order_unreadable"
    # Unique stale ids stay allowed.
    path.write_text(json.dumps({"order": [a, "gone"]}) + "\n", encoding="utf-8")
    assert tree_order.load_order(path) == [a, "gone"]


def test_storage_error_on_put_is_a_structured_error(
    client: TestClient, db_path: Path
) -> None:
    """Codex P2 then P1 on #6186: a real failed store explains itself.

    No monkeypatch: a non-empty directory sits where the overlay file goes, so
    the production save_order writes its temp file and the real os.replace
    fails (IsADirectoryError on POSIX, PermissionError on Windows). Unlike a
    chmod, this fails for root too, so the test never needs a skip.
    """
    a = _create(client, "A")
    path = tree_order.path_for(db_path.parent.parent)
    path.mkdir(parents=True)
    (path / "keep").write_text("blocks the replace\n", encoding="utf-8")
    r = client.put("/api/v1/playlist-tree/order", json={"order": [a]})
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "playlist_tree_order_unwritable"
    assert detail["message"].startswith("could not store the playlist order: ")
    # The failed write left no temp file behind and did not touch the blocker.
    assert sorted(p.name for p in path.parent.iterdir() if p.name.startswith(f".{path.name}.")) == []
    assert (path / "keep").read_text(encoding="utf-8") == "blocks the replace\n"


def test_playlists_left_out_follow_the_order(client: TestClient) -> None:
    a = _create(client, "A")
    _create(client, "B")
    assert client.put("/api/v1/playlist-tree/order", json={"order": [a]}).status_code == 200
    # A new playlist made after the reorder still shows, after the ordered ones.
    _create(client, "New")
    assert _seqs(client) == {"A": 0, "B": None, "New": None}


@pytest.mark.parametrize(
    ("make_order", "needle"),
    [
        (lambda a: [a, a], "appears twice"),
        (lambda a: [a, "nope"], "unknown playlist ids: nope"),
    ],
)
def test_bad_order_is_refused_and_not_written(
    client: TestClient, db_path: Path, make_order, needle: str
) -> None:
    a = _create(client, "A")
    r = client.put("/api/v1/playlist-tree/order", json={"order": make_order(a)})
    assert r.status_code == 422
    assert needle in r.json()["detail"]["message"]
    assert not tree_order.path_for(db_path.parent.parent).exists()


def test_deleted_playlist_drops_out_of_seq(client: TestClient) -> None:
    a = _create(client, "A")
    b = _create(client, "B")
    assert client.put("/api/v1/playlist-tree/order", json={"order": [b, a]}).status_code == 200
    etag = client.get(f"/api/v1/playlists/{b}").headers["ETag"]
    assert client.delete(f"/api/v1/playlists/{b}", headers={"If-Match": etag}).status_code == 204
    # The stale id stays in the file but names nothing; A keeps its slot.
    assert _seqs(client) == {"A": 1}


@pytest.mark.requirement("LIBTREE-03")
def test_corrupt_file_fails_loud(client: TestClient, db_path: Path) -> None:
    _create(client, "A")
    path = tree_order.path_for(db_path.parent.parent)
    path.write_text("{not json", encoding="utf-8")
    r = client.get("/api/v1/playlists")
    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "playlist_tree_order_unreadable"


def test_seq_for_offsets_rekordbox_seq_after_user_order() -> None:
    out = tree_order.seq_for(
        ["u1", "rb1", "rb2", "own"],
        ["u1"],
        {"u1": 5, "rb1": 0, "rb2": 1, "own": None},
    )
    assert out == {"u1": 0, "rb1": 1, "rb2": 2, "own": None}
