"""STATE-19: a settled identity remap takes no writer lock on later CloudSync rounds.

[if] every loser's children already moved [then] prepare_spoke_identity writes nothing, [else stop].
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.sync_hub import client
from apps.sync_hub.engine_identity_map import prepare_spoke_identity
from tests.cloudsync.test_hub_sync import _DEV_A, _T0, _T1
from tests.cloudsync.test_track_identity_collapse import (
    _HASH_A,
    _insert_identified_track,
    _insert_location,
)

pytestmark = pytest.mark.requirement("STATE-19")

_LOSER = "trk-settled-dup"
_SURVIVOR = "trk-settled-keep"
_WRITES = ("BEGIN", "INSERT", "UPDATE", "DELETE", "REPLACE", "SAVEPOINT")


def _seed_duplicate_pair(conn: sqlite3.Connection) -> None:
    _insert_identified_track(
        conn, _LOSER, title="older copy", content_hash=_HASH_A,
        updated_at=_T0, origin=_DEV_A, file_path="/Silver/a.mp3",
    )
    _insert_identified_track(
        conn, _SURVIVOR, title="newer copy", content_hash=_HASH_A,
        updated_at=_T1, origin=_DEV_A, file_path="/Silver/b.mp3",
    )
    _insert_location(
        conn, location_id="loc-loser", stable_id=_LOSER,
        file_path="/Silver/a.mp3", updated_at=_T0, origin=_DEV_A,
    )
    conn.commit()


@contextmanager
def _writes_issued(conn: sqlite3.Connection) -> Iterator[list[str]]:
    issued: list[str] = []

    def trace(sql: str) -> None:
        head = " ".join(sql.split()).upper()
        if head.startswith(_WRITES):
            issued.append(head[:60])

    conn.set_trace_callback(trace)
    try:
        yield issued
    finally:
        conn.set_trace_callback(None)


def _location_owner(conn: sqlite3.Connection, location_id: str) -> str:
    return str(
        conn.execute(
            "SELECT stable_id FROM track_locations WHERE location_id = ?", (location_id,)
        ).fetchone()[0]
    )


@pytest.mark.requirement("STATE-19")
def test_a_settled_remap_issues_no_write_and_no_lock(tmp_path: Path) -> None:
    """[if] the loser's children already moved [then] a re-run issues no write, [else stop]."""
    db = client.state_db_path(tmp_path / "spoke")
    conn = state_db.open_rw(db)
    try:
        _seed_duplicate_pair(conn)
        assert prepare_spoke_identity(conn) == 1, "control: the first round must move the child"
        assert _location_owner(conn, "loc-loser") == _SURVIVOR

        holder = sqlite3.connect(str(db), isolation_level=None, timeout=0)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with _writes_issued(conn) as issued:
                moved = prepare_spoke_identity(conn)
        finally:
            holder.execute("ROLLBACK")
            holder.close()
    finally:
        conn.close()

    assert moved == 0
    assert issued == [], f"a settled round still wrote: {issued[:5]}"


@pytest.mark.requirement("STATE-19")
def test_a_child_added_to_a_settled_loser_is_still_moved(tmp_path: Path) -> None:
    """[if] a settled loser gains a new child [then] the next round moves it, [else stop]."""
    conn = state_db.open_rw(client.state_db_path(tmp_path / "spoke"))
    try:
        _seed_duplicate_pair(conn)
        assert prepare_spoke_identity(conn) == 1
        _insert_location(
            conn, location_id="loc-late", stable_id=_LOSER,
            file_path="/Silver/late.mp3", updated_at=_T1, origin=_DEV_A,
        )
        conn.commit()

        assert prepare_spoke_identity(conn) == 1
        assert _location_owner(conn, "loc-late") == _SURVIVOR
    finally:
        conn.close()
