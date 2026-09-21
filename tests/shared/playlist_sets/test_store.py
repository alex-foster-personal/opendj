"""Store-layer tests for playlist sets (SET-05).

[if] a set is performed or practiced [then] play_count and runs update correctly, [else stop].
[if] playlist_sets store semantics drift from SET-05 [then] fail, [else stop].
"""
from __future__ import annotations

import sqlite3

import pytest

from apps.shared.play_orders import add_entry, create_play_order
from apps.shared.playlist_sets import (
    create_playlist_set,
    list_playlist_sets,
    load_playlist_set,
    record_run,
)
from apps.shared.playlist_sets.schema import apply_playlist_set_migrations
from tests.shared.playlist_sets.conftest import seed_playlist_and_tracks

pytestmark = pytest.mark.requirement("SET-05")


def test_list_several_sets(ps_conn: sqlite3.Connection) -> None:
    """[if] a playlist holds several sets [then] both names and play_count==0."""
    create_playlist_set(ps_conn, "pl1", "AtlantisParty")
    create_playlist_set(ps_conn, "pl1", "Warmup")
    sets = list_playlist_sets(ps_conn, "pl1")
    assert {s.name for s in sets} == {"AtlantisParty", "Warmup"}
    assert all(s.play_count == 0 for s in sets)


def test_performance_increments_without_mutating_membership(
    ps_conn_with_memberships: sqlite3.Connection,
) -> None:
    """[if] a set is performed [then] play_count is 1 and memberships unchanged."""
    conn = ps_conn_with_memberships
    seed_playlist_and_tracks(conn, "pl1", ["t-a", "t-b"])
    conn.executemany(
        "INSERT INTO playlist_memberships(playlist_id, stable_id, position) VALUES (?, ?, ?)",
        [("pl1", "t-a", 0), ("pl1", "t-b", 1)],
    )
    before = conn.execute(
        "SELECT playlist_id, stable_id, position FROM playlist_memberships "
        "WHERE playlist_id=? ORDER BY position",
        ("pl1",),
    ).fetchall()
    po_count_before = conn.execute("SELECT COUNT(*) FROM play_orders").fetchone()[0]
    set_id = create_playlist_set(conn, "pl1", "Gig")
    record_run(conn, set_id, "performance")
    ps = load_playlist_set(conn, set_id)
    after = conn.execute(
        "SELECT playlist_id, stable_id, position FROM playlist_memberships "
        "WHERE playlist_id=? ORDER BY position",
        ("pl1",),
    ).fetchall()
    po_count_after = conn.execute("SELECT COUNT(*) FROM play_orders").fetchone()[0]
    assert ps.play_count == 1
    assert before == after
    assert po_count_before == po_count_after


def test_practice_then_performance(
    ps_conn: sqlite3.Connection,
) -> None:
    """[if] the user is practicing [then] practice run exists and play_count stays 0."""
    set_id = create_playlist_set(ps_conn, "pl1", "PracticeSet")
    record_run(ps_conn, set_id, "practice")
    ps = load_playlist_set(ps_conn, set_id, include_runs=True)
    assert ps.play_count == 0
    assert len(ps.runs) == 1
    assert ps.runs[0].kind == "practice"
    record_run(ps_conn, set_id, "performance")
    ps = load_playlist_set(ps_conn, set_id, include_runs=True)
    assert ps.play_count == 1
    assert [r.kind for r in ps.runs] == ["practice", "performance"]


def test_create_from_play_order_snapshot_is_independent(
    ps_conn_with_memberships: sqlite3.Connection,
) -> None:
    conn = ps_conn_with_memberships
    seed_playlist_and_tracks(conn, "pl1", ["t-a", "t-b"])
    conn.executemany(
        "INSERT INTO playlist_memberships(playlist_id, stable_id, position) VALUES (?, ?, ?)",
        [("pl1", "t-a", 0), ("pl1", "t-b", 1)],
    )
    po_id = create_play_order(conn, "pl1", "peak")
    add_entry(conn, po_id, "t-a", 0)
    add_entry(conn, po_id, "t-b", 1)
    set_id = create_playlist_set(conn, "pl1", "FromOrder", from_play_order="peak")
    before = [e.stable_id for e in load_playlist_set(conn, set_id).entries]
    add_entry(conn, po_id, "t-c", 2)
    after = [e.stable_id for e in load_playlist_set(conn, set_id).entries]
    assert before == ["t-a", "t-b"]
    assert after == before


def test_migrations_idempotent(ps_conn: sqlite3.Connection) -> None:
    assert apply_playlist_set_migrations(ps_conn) == 1
    assert apply_playlist_set_migrations(ps_conn) == 1
    tables = {
        r[0]
        for r in ps_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "playlist_sets" in tables
    assert "play_orders" in tables
