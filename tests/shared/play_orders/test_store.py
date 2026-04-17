"""Store-layer tests for play-orders (PLAY-01)."""
from __future__ import annotations

import sqlite3

import pytest

from apps.shared.play_orders import (
    DEFAULT_ORDER_NAME,
    add_entry,
    create_play_order,
    delete_play_order,
    list_play_orders,
    load_play_order,
)

pytestmark = pytest.mark.requirement("PLAY-01")


def test_round_trip(po_conn: sqlite3.Connection) -> None:
    """create + two entries (one with overrides) + load -> identical shape."""
    po_id = create_play_order(
        po_conn, playlist_id="pl1", name="warmup", generated_by="manual"
    )
    add_entry(
        po_conn,
        po_id,
        stable_id="t-abc",
        position=0,
        transition_hint="bpm-match",
    )
    add_entry(
        po_conn,
        po_id,
        stable_id="t-def",
        position=1,
        target_key="8A",
        target_tempo=128.5,
        key_sync=True,
        transition_hint="camelot-step-2",
    )
    po = load_play_order(po_conn, "pl1", "warmup")
    assert po.id == po_id
    assert po.name == "warmup"
    assert po.generated_by == "manual"
    assert po.schema_version == 1
    assert [e.stable_id for e in po.entries] == ["t-abc", "t-def"]
    assert po.entries[0].target_key is None
    assert po.entries[0].transition_hint == "bpm-match"
    assert po.entries[1].target_key == "8A"
    assert po.entries[1].target_tempo == pytest.approx(128.5)
    assert po.entries[1].key_sync is True
    assert po.entries[1].transition_hint == "camelot-step-2"


def test_unique_name(po_conn: sqlite3.Connection) -> None:
    """Two play-orders sharing (playlist_id, name) raise IntegrityError."""
    create_play_order(po_conn, "pl1", "peak")
    with pytest.raises(sqlite3.IntegrityError):
        create_play_order(po_conn, "pl1", "peak")


def test_default_virtual_when_empty(po_conn: sqlite3.Connection) -> None:
    """list_play_orders returns only the virtual default when empty."""
    assert list_play_orders(po_conn, "pl-fresh") == [DEFAULT_ORDER_NAME]


def test_default_virtual_read_through(
    po_conn_with_memberships: sqlite3.Connection,
) -> None:
    """load_play_order(playlist_id, "default") reads playlist_memberships."""
    conn = po_conn_with_memberships
    conn.executemany(
        "INSERT INTO playlist_memberships(playlist_id, stable_id, position) "
        "VALUES (?, ?, ?)",
        [("pl42", "t-a", 0), ("pl42", "t-b", 1), ("pl42", "t-c", 2)],
    )
    po = load_play_order(conn, "pl42", DEFAULT_ORDER_NAME)
    assert po.name == DEFAULT_ORDER_NAME
    assert po.id is None
    assert [e.stable_id for e in po.entries] == ["t-a", "t-b", "t-c"]


def test_list_includes_named_orders(po_conn: sqlite3.Connection) -> None:
    create_play_order(po_conn, "pl1", "peak")
    create_play_order(po_conn, "pl1", "warmup")
    names = list_play_orders(po_conn, "pl1")
    assert names[0] == DEFAULT_ORDER_NAME
    assert sorted(names[1:]) == ["peak", "warmup"]


def test_create_reserved_name_raises(po_conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="reserved"):
        create_play_order(po_conn, "pl1", DEFAULT_ORDER_NAME)


def test_delete_cascades_entries(po_conn: sqlite3.Connection) -> None:
    po_id = create_play_order(po_conn, "pl1", "x")
    add_entry(po_conn, po_id, "t-1", 0)
    add_entry(po_conn, po_id, "t-2", 1)
    delete_play_order(po_conn, "pl1", "x")
    rows = po_conn.execute(
        "SELECT COUNT(*) FROM play_order_entries WHERE play_order_id=?",
        (po_id,),
    ).fetchone()
    assert rows[0] == 0
    with pytest.raises(LookupError):
        load_play_order(po_conn, "pl1", "x")


def test_delete_default_noop(po_conn: sqlite3.Connection) -> None:
    """Deleting the virtual default must not raise; it is a no-op."""
    delete_play_order(po_conn, "pl1", DEFAULT_ORDER_NAME)


def test_add_entry_unknown_play_order_raises(
    po_conn: sqlite3.Connection,
) -> None:
    # Migration runs on first create; ensure tables exist first.
    create_play_order(po_conn, "pl1", "seed")
    with pytest.raises(sqlite3.IntegrityError):
        add_entry(po_conn, 999_999, "t-1", 0)


def test_add_entry_invalid_position(po_conn: sqlite3.Connection) -> None:
    po_id = create_play_order(po_conn, "pl1", "x")
    with pytest.raises(ValueError, match="position"):
        add_entry(po_conn, po_id, "t-1", -1)


def test_duplicate_position_rejected(po_conn: sqlite3.Connection) -> None:
    po_id = create_play_order(po_conn, "pl1", "x")
    add_entry(po_conn, po_id, "t-1", 0)
    with pytest.raises(sqlite3.IntegrityError):
        add_entry(po_conn, po_id, "t-2", 0)


def test_event_emitted_on_create(
    po_conn_with_events: sqlite3.Connection,
) -> None:
    conn = po_conn_with_events
    create_play_order(conn, "pl1", "x", generated_by="manual")
    rows = conn.execute(
        "SELECT kind, payload_json FROM events WHERE kind='play_order_changed'"
    ).fetchall()
    assert len(rows) == 1
    assert '"action":"created"' in rows[0][1]


def test_event_emitted_on_update(
    po_conn_with_events: sqlite3.Connection,
) -> None:
    conn = po_conn_with_events
    po_id = create_play_order(conn, "pl1", "x")
    add_entry(conn, po_id, "t-1", 0)
    actions = [
        row[0]
        for row in conn.execute(
            "SELECT json_extract(payload_json, '$.action') FROM events "
            "WHERE kind='play_order_changed' ORDER BY id ASC"
        ).fetchall()
    ]
    assert actions == ["created", "updated"]


def test_event_missing_is_silent(po_conn: sqlite3.Connection) -> None:
    """When the events table is absent, create still succeeds."""
    po_id = create_play_order(po_conn, "pl1", "x")
    assert po_id > 0
