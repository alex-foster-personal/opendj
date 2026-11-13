"""move-copy-between-playlists node: atomic POST .../tracks/transfer.

Regression one-liners:
  * if add (copy) does not preserve source membership then broken
  * if move does not remove the identity from source then broken
  * if self-drop (source == dest) does not 422 then broken
  * if an interrupt between dest and source writes leaves both lists dirty then broken
  * if a stale dest etag 409s but source was already written then broken
  * if a move naming a non-member of source writes either list then broken
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import library_edits
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.playlist_store import PlaylistStore
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

TRACK_IDS: list[str] = ["t-001", "t-002", "t-003", "t-004"]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
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


def _create(client: TestClient, name: str) -> tuple[str, str]:
    r = client.post("/api/v1/playlists", json={"name": name})
    assert r.status_code == 201, r.text
    body = r.json()
    return body["playlist_id"], r.headers["ETag"]


def _put_tracks(client: TestClient, pid: str, etag: str, stable_ids: list[str]) -> str:
    r = client.put(
        f"/api/v1/playlists/{pid}/tracks",
        json={"stable_ids": stable_ids},
        headers={"If-Match": etag},
    )
    assert r.status_code == 200, r.text
    return r.headers["ETag"]


def _get_items(client: TestClient, pid: str) -> list[str]:
    r = client.get(f"/api/v1/playlists/{pid}")
    assert r.status_code == 200, r.text
    return [t["stable_id"] for t in r.json()["tracks"]]


def _get_membership_edit_ids(db_path: Path, playlist_id: str) -> list[str | None]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT edit_id FROM playlist_memberships "
                "WHERE playlist_id = ? AND deleted_at IS NULL "
                "ORDER BY COALESCE(order_key, printf('%08d', position)), position",
                (playlist_id,),
            )
        ]
    finally:
        conn.close()


def _get_etag(client: TestClient, pid: str) -> str:
    r = client.get(f"/api/v1/playlists/{pid}")
    assert r.status_code == 200, r.text
    etag = r.headers.get("ETag")
    assert etag, "GET must carry ETag"
    return etag


def _transfer(
    client: TestClient,
    dest_id: str,
    dest_etag: str,
    *,
    stable_ids: list[str],
    mode: str,
    source_id: str | None = None,
    source_etag: str | None = None,
):
    body: dict = {"stable_ids": stable_ids, "mode": mode}
    if source_id is not None:
        body["source_playlist_id"] = source_id
    if source_etag is not None:
        body["source_etag"] = source_etag
    return client.post(
        f"/api/v1/playlists/{dest_id}/tracks/transfer",
        json=body,
        headers={"If-Match": dest_etag},
    )


def _event_kinds(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [row[0] for row in conn.execute("SELECT kind FROM events ORDER BY id")]
    finally:
        conn.close()


def _event_actors(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[0] for row in conn.execute("SELECT DISTINCT actor FROM events WHERE kind LIKE 'playlist.%'")}
    finally:
        conn.close()


# --- copy (add) identity ---------------------------------------------------


def test_copy_adds_to_dest_preserves_source(client: TestClient) -> None:
    pid_a, etag_a = _create(client, "A")
    pid_b, etag_b = _create(client, "B")
    etag_a = _put_tracks(client, pid_a, etag_a, ["t-001", "t-002"])
    etag_b = _put_tracks(client, pid_b, etag_b, ["t-003"])

    r = _transfer(client, pid_b, etag_b, stable_ids=["t-001"], mode="add")
    assert r.status_code == 200, r.text
    assert _get_items(client, pid_b) == ["t-003", "t-001"]
    assert _get_items(client, pid_a) == ["t-001", "t-002"]


@pytest.mark.requirement("LIBEDIT-9179")
def test_transfer_preserves_edit_membership_on_copy(client: TestClient, db_path: Path) -> None:
    """[if] a playlist row names an edit [then] adding a plain track must not drop it, [else stop]."""
    pid_b, etag_b = _create(client, "B-with-edit")
    etag_b = _put_tracks(client, pid_b, etag_b, ["t-001"])
    conn = state_db.open_rw(db_path)
    try:
        writer = StateWriter(conn, bus=FakeEventBus(), actor="edit-seed")
        try:
            library_edits.create_edit(
                writer,
                "xfer-session",
                source_track_id="t-001",
                label="Cut",
                in_ms=1_000,
                out_ms=10_000,
                created_by="tests",
                playlist_id=pid_b,
            )
        finally:
            writer.close()
    finally:
        conn.close()

    etag_b = _get_etag(client, pid_b)
    before_edits = _get_membership_edit_ids(db_path, pid_b)
    assert before_edits[0] is None
    assert before_edits[1] is not None

    r = _transfer(client, pid_b, etag_b, stable_ids=["t-002"], mode="add")
    assert r.status_code == 200, r.text

    after = _get_membership_edit_ids(db_path, pid_b)
    assert after[: len(before_edits)] == before_edits
    assert len(after) == len(before_edits) + 1


def test_copy_skip_already_present_is_idempotent(client: TestClient) -> None:
    pid_b, etag_b = _create(client, "B")
    etag_b = _put_tracks(client, pid_b, etag_b, ["t-003"])
    etag_before = _get_etag(client, pid_b)

    r = _transfer(client, pid_b, etag_b, stable_ids=["t-003"], mode="add")
    assert r.status_code == 200, r.text
    assert _get_items(client, pid_b) == ["t-003"]
    assert _get_etag(client, pid_b) == etag_before


# --- move identity ---------------------------------------------------------


def test_move_removes_from_source_adds_to_dest(client: TestClient) -> None:
    pid_a, etag_a = _create(client, "A")
    pid_b, etag_b = _create(client, "B")
    etag_a = _put_tracks(client, pid_a, etag_a, ["t-001", "t-002"])
    etag_b = _put_tracks(client, pid_b, etag_b, ["t-003"])

    r = _transfer(
        client,
        pid_b,
        etag_b,
        stable_ids=["t-001"],
        mode="move",
        source_id=pid_a,
        source_etag=etag_a,
    )
    assert r.status_code == 200, r.text
    assert _get_items(client, pid_b) == ["t-003", "t-001"]
    assert _get_items(client, pid_a) == ["t-002"]


def test_move_already_in_dest_still_removes_from_source(client: TestClient) -> None:
    pid_a, etag_a = _create(client, "A")
    pid_b, etag_b = _create(client, "B")
    etag_a = _put_tracks(client, pid_a, etag_a, ["t-001"])
    etag_b = _put_tracks(client, pid_b, etag_b, ["t-001", "t-002"])

    r = _transfer(
        client,
        pid_b,
        etag_b,
        stable_ids=["t-001"],
        mode="move",
        source_id=pid_a,
        source_etag=etag_a,
    )
    assert r.status_code == 200, r.text
    assert _get_items(client, pid_b) == ["t-001", "t-002"]
    assert _get_items(client, pid_a) == []


# --- validation ------------------------------------------------------------


def test_cycle_self_drop_rejected(client: TestClient) -> None:
    pid, etag = _create(client, "Self")
    etag = _put_tracks(client, pid, etag, ["t-001"])

    r = _transfer(
        client,
        pid,
        etag,
        stable_ids=["t-001"],
        mode="move",
        source_id=pid,
        source_etag=etag,
    )
    assert r.status_code == 422, r.text
    assert _get_items(client, pid) == ["t-001"]


def test_move_without_source_rejected(client: TestClient) -> None:
    pid_b, etag_b = _create(client, "B")
    etag_b = _put_tracks(client, pid_b, etag_b, ["t-003"])

    r = _transfer(client, pid_b, etag_b, stable_ids=["t-001"], mode="move")
    assert r.status_code == 422, r.text
    assert _get_items(client, pid_b) == ["t-003"]


def test_missing_dest_if_match_returns_428(client: TestClient) -> None:
    pid, _ = _create(client, "X")
    r = client.post(
        f"/api/v1/playlists/{pid}/tracks/transfer",
        json={"stable_ids": ["t-001"], "mode": "add"},
    )
    assert r.status_code == 428, r.text


def test_stale_dest_etag_409_source_unchanged(client: TestClient) -> None:
    pid_a, etag_a = _create(client, "A")
    pid_b, etag_b = _create(client, "B")
    etag_a = _put_tracks(client, pid_a, etag_a, ["t-001"])
    etag_b = _put_tracks(client, pid_b, etag_b, [])

    r = _transfer(
        client,
        pid_b,
        '"stale-dest"',
        stable_ids=["t-001"],
        mode="move",
        source_id=pid_a,
        source_etag=etag_a,
    )
    assert r.status_code == 409, r.text
    assert r.json()["current"]["playlist_id"] == pid_b
    assert _get_items(client, pid_a) == ["t-001"]
    assert _get_items(client, pid_b) == []


def test_stale_source_etag_409_dest_unchanged(client: TestClient) -> None:
    pid_a, etag_a = _create(client, "A")
    pid_b, etag_b = _create(client, "B")
    etag_a = _put_tracks(client, pid_a, etag_a, ["t-001"])
    etag_b = _put_tracks(client, pid_b, etag_b, [])

    r = _transfer(
        client,
        pid_b,
        etag_b,
        stable_ids=["t-001"],
        mode="move",
        source_id=pid_a,
        source_etag='"stale-source"',
    )
    assert r.status_code == 409, r.text
    assert r.json()["current"]["playlist_id"] == pid_a
    assert _get_items(client, pid_a) == ["t-001"]
    assert _get_items(client, pid_b) == []


def test_unknown_stable_id_422_neither_written(client: TestClient) -> None:
    pid_a, etag_a = _create(client, "A")
    pid_b, etag_b = _create(client, "B")
    etag_a = _put_tracks(client, pid_a, etag_a, ["t-001"])
    etag_b = _put_tracks(client, pid_b, etag_b, ["t-002"])

    r = _transfer(
        client,
        pid_b,
        etag_b,
        stable_ids=["t-missing"],
        mode="move",
        source_id=pid_a,
        source_etag=etag_a,
    )
    assert r.status_code == 422, r.text
    assert _get_items(client, pid_a) == ["t-001"]
    assert _get_items(client, pid_b) == ["t-002"]


def test_move_member_not_in_source_422_neither_written(client: TestClient) -> None:
    """A move naming one id that is not in the source rejects and leaves both lists unchanged."""
    pid_a, etag_a = _create(client, "A")
    pid_b, etag_b = _create(client, "B")
    etag_a = _put_tracks(client, pid_a, etag_a, ["t-001", "t-002"])
    etag_b = _put_tracks(client, pid_b, etag_b, ["t-003"])

    r = _transfer(
        client,
        pid_b,
        etag_b,
        stable_ids=["t-001", "t-004"],
        mode="move",
        source_id=pid_a,
        source_etag=etag_a,
    )
    assert r.status_code == 422, r.text
    assert "t-004" in r.text
    assert _get_items(client, pid_a) == ["t-001", "t-002"]
    assert _get_items(client, pid_b) == ["t-003"]
    assert _get_etag(client, pid_a) == etag_a
    assert _get_etag(client, pid_b) == etag_b


def test_multi_row_move_preserves_request_order_and_source_rest(client: TestClient) -> None:
    """A valid multi-row move appends in request order to dest and removes exactly those rows from source."""
    pid_a, etag_a = _create(client, "A")
    pid_b, etag_b = _create(client, "B")
    etag_a = _put_tracks(client, pid_a, etag_a, ["t-001", "t-002", "t-003"])
    etag_b = _put_tracks(client, pid_b, etag_b, ["t-004"])

    r = _transfer(
        client,
        pid_b,
        etag_b,
        stable_ids=["t-003", "t-001"],
        mode="move",
        source_id=pid_a,
        source_etag=etag_a,
    )
    assert r.status_code == 200, r.text
    assert _get_items(client, pid_b) == ["t-004", "t-003", "t-001"]
    assert _get_items(client, pid_a) == ["t-002"]


def test_multi_row_copy_keeps_source_and_skips_present(client: TestClient) -> None:
    """A valid multi-row copy appends only missing rows to dest and leaves source untouched."""
    pid_a, etag_a = _create(client, "A")
    pid_b, etag_b = _create(client, "B")
    etag_a = _put_tracks(client, pid_a, etag_a, ["t-001", "t-002", "t-003"])
    etag_b = _put_tracks(client, pid_b, etag_b, ["t-002"])

    r = _transfer(
        client,
        pid_b,
        etag_b,
        stable_ids=["t-003", "t-002", "t-001"],
        mode="add",
        source_id=pid_a,
    )
    assert r.status_code == 200, r.text
    assert _get_items(client, pid_b) == ["t-002", "t-003", "t-001"]
    assert _get_items(client, pid_a) == ["t-001", "t-002", "t-003"]
    assert _get_etag(client, pid_a) == etag_a


def test_unknown_source_playlist_404(client: TestClient) -> None:
    pid_b, etag_b = _create(client, "B")
    etag_b = _put_tracks(client, pid_b, etag_b, [])

    r = _transfer(
        client,
        pid_b,
        etag_b,
        stable_ids=["t-001"],
        mode="move",
        source_id="pl-does-not-exist",
        source_etag='"x"',
    )
    assert r.status_code == 404, r.text


# --- rollback / events / de-dupe -------------------------------------------


def test_interrupt_second_write_rolls_back(db_path: Path) -> None:
    store = PlaylistStore(db_path, bus=FakeEventBus(), actor="webui")
    try:
        pa = store.create_playlist("A")
        pb = store.create_playlist("B")
        store.replace_memberships(pa.playlist_id, ["t-001", "t-002"], expected_etag=pa.etag)
        store.replace_memberships(pb.playlist_id, ["t-003"], expected_etag=pb.etag)
        pa = store.get_playlist_row(pa.playlist_id)
        pb = store.get_playlist_row(pb.playlist_id)

        calls = 0
        original = store._writer.set_playlist_memberships

        def boom(
            playlist_id: str,
            stable_ids: list[str],
            edit_ids: list[str | None] | None = None,
        ) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated interrupt")
            original(playlist_id, stable_ids, edit_ids)

        with patch.object(store._writer, "set_playlist_memberships", side_effect=boom):
            with pytest.raises(RuntimeError, match="simulated interrupt"):
                store.transfer_memberships(
                    pb.playlist_id,
                    ["t-001"],
                    dest_etag=pb.etag,
                    mode="move",
                    source_id=pa.playlist_id,
                    source_etag=pa.etag,
                )

        assert store.get_playlist_row(pa.playlist_id).items == ["t-001", "t-002"]
        assert store.get_playlist_row(pb.playlist_id).items == ["t-003"]
    finally:
        store.close()


def test_move_appends_membership_events_with_webui_actor(
    client: TestClient,
    db_path: Path,
) -> None:
    pid_a, etag_a = _create(client, "A")
    pid_b, etag_b = _create(client, "B")
    etag_a = _put_tracks(client, pid_a, etag_a, ["t-001"])
    etag_b = _put_tracks(client, pid_b, etag_b, [])

    kinds_before = _event_kinds(db_path)
    r = _transfer(
        client,
        pid_b,
        etag_b,
        stable_ids=["t-001"],
        mode="move",
        source_id=pid_a,
        source_etag=etag_a,
    )
    assert r.status_code == 200, r.text
    kinds_after = _event_kinds(db_path)
    new_kinds = kinds_after[len(kinds_before) :]
    assert new_kinds.count("playlist.memberships.set") == 2
    assert _event_actors(db_path) == {"webui"}


def test_duplicate_ids_in_request_append_once(client: TestClient) -> None:
    pid_b, etag_b = _create(client, "B")
    etag_b = _put_tracks(client, pid_b, etag_b, [])

    r = _transfer(
        client,
        pid_b,
        etag_b,
        stable_ids=["t-001", "t-001", "t-002"],
        mode="add",
    )
    assert r.status_code == 200, r.text
    assert _get_items(client, pid_b) == ["t-001", "t-002"]
