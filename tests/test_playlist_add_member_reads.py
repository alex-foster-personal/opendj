"""Adding to a playlist reads each existing member once (LIBM-129, #3963).

Found at 10k members (LIBM-120 measurement, Fri 25 Sep 2026): one
``items:add`` cost 634 ms against 5.8 ms at 50 members, because the add read
every existing member THREE times - a full load for the before-snapshot, a
second full load of seven columns just to find two neighbor order_keys, and
a third full load for the response.

The response contract (``PlaylistWriteOut.items``) and the undo history's
before/after snapshots both carry the whole membership, so one ordered read
of it is the floor this endpoint can reach without a contract change. The
instrument is a counting ``row_factory`` on the store's REAL connection: an
add to a 400-member playlist may materialize exactly 360 more rows than an
add to a 40-member one, one per extra member, never two or three.

Regression one-liners:
  - if adding one track reads an existing member more than once then broken
  - if the bounded neighbor lookup picks different order_keys than the full member list then broken
  - if the undo snapshot or response items differ from an independent membership read then broken
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.webui.server.playlist_add import _load_live_members, _neighbor_order_keys
from apps.webui.server.playlist_store import PlaylistStore
from tests.webui.sql_trace import RowCounter

pytestmark = pytest.mark.requirement("LIBM-129")

SMALL, LARGE = 40, 400
NOW = "2026-09-25T00:00:00Z"


def _sid(i: int) -> str:
    return f"{i:040x}"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[PlaylistStore]:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    conn.executemany(
        "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, file_path, "
        "created_at, updated_at) VALUES (?, 'inferred', 1000, NULL, ?, ?)",
        [(_sid(i), NOW, NOW) for i in range(LARGE + 10)],
    )
    conn.commit()
    conn.close()
    playlist_store = PlaylistStore(path, bus=FakeEventBus())
    yield playlist_store
    playlist_store.close()


def _playlist(store: PlaylistStore, size: int) -> str:
    row = store.create_playlist(f"{size} members")
    row = store.replace_memberships(
        row.playlist_id, [_sid(i) for i in range(size)], expected_etag=row.etag,
    )
    return row.playlist_id


def _rows_read_by_one_add(store: PlaylistStore, playlist_id: str, position: int | None) -> int:
    counter = RowCounter()
    store._conn.row_factory = counter
    try:
        row = store.add_memberships(playlist_id, [_sid(LARGE + 1)], position=position)
    finally:
        store._conn.row_factory = sqlite3.Row
    assert row.items.count(_sid(LARGE + 1)) == 1
    return counter.rows


@pytest.mark.parametrize("where", ["append", "middle"])
def test_one_add_reads_each_existing_member_once(store: PlaylistStore, where: str) -> None:
    """[if] one track is added to a 400-member playlist [then] each existing member is read once, [else stop]."""
    small = _playlist(store, SMALL)
    large = _playlist(store, LARGE)
    small_rows = _rows_read_by_one_add(store, small, None if where == "append" else SMALL // 2)
    large_rows = _rows_read_by_one_add(store, large, None if where == "append" else LARGE // 2)
    assert small_rows > SMALL, "counter saw no member rows: the instrument is not attached"
    per_extra_member = (large_rows - small_rows) / (LARGE - SMALL)
    assert per_extra_member == 1, (
        f"an add read {small_rows} rows at {SMALL} members and {large_rows} at "
        f"{LARGE}: {per_extra_member:g} reads per existing member, want exactly 1 "
        "(the response items / undo snapshot)"
    )


def test_neighbor_lookup_matches_the_full_member_list(store: PlaylistStore) -> None:
    """[if] neighbors are read by a bounded query [then] they are the full list's neighbors, [else stop]."""
    playlist_id = _playlist(store, 12)
    conn = store._conn
    # Legacy rows with no order_key sort by their zero-padded position, the
    # fallback both readers must apply identically.
    conn.execute(
        "UPDATE playlist_memberships SET order_key = NULL "
        "WHERE playlist_id = ? AND position IN (0, 5, 11)",
        (playlist_id,),
    )
    store.remove_memberships(playlist_id, [_load_live_members(conn, playlist_id)[3].item_id])
    members = _load_live_members(conn, playlist_id)
    assert len(members) == 11
    for index in range(len(members) + 1):
        expected = (
            members[index - 1].order_key if index > 0 else None,
            members[index].order_key if index < len(members) else None,
        )
        assert _neighbor_order_keys(conn, playlist_id, index, len(members)) == expected, index


def test_response_and_undo_snapshot_match_independent_reads(store: PlaylistStore) -> None:
    """[if] the before-snapshot is derived from the after read [then] it equals the membership before the add, [else stop]."""
    playlist_id = _playlist(store, 30)
    conn = store._conn
    before = [m.stable_id for m in _load_live_members(conn, playlist_id)]
    row = store.add_memberships(playlist_id, [_sid(LARGE + 2), _sid(3)], position=7)
    after = [m.stable_id for m in _load_live_members(conn, playlist_id)]
    assert row.items == after
    assert after == before[:7] + [_sid(LARGE + 2), _sid(3)] + before[7:]
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM events WHERE kind = 'playlist.edit' ORDER BY id DESC LIMIT 1"
    ).fetchone()[0])
    assert payload["op"] == "memberships"
    assert payload["before"]["items"] == before
    assert payload["after"]["items"] == after
