"""PLAY-03 override write tests."""
from __future__ import annotations

import sqlite3

import pytest

from apps.dj_copilot.overrides import clear_override, set_override
from apps.shared.play_orders import (
    add_entry,
    create_play_order,
    load_play_order,
)

pytestmark = pytest.mark.requirement("PLAY-03")


def _seed_order(conn: sqlite3.Connection) -> int:
    po_id = create_play_order(conn, "pl", "x")
    add_entry(conn, po_id, "t-0", 0)
    add_entry(conn, po_id, "t-1", 1)
    return po_id


def test_set_override_key_only(po_conn: sqlite3.Connection) -> None:
    po_id = _seed_order(po_conn)
    set_override(po_conn, po_id, 1, target_key="8A")
    loaded = load_play_order(po_conn, "pl", "x")
    assert loaded.entries[1].target_key == "8A"
    assert loaded.entries[1].target_tempo is None


def test_set_override_multiple_fields(po_conn: sqlite3.Connection) -> None:
    po_id = _seed_order(po_conn)
    set_override(
        po_conn,
        po_id,
        0,
        target_key="8A",
        target_tempo=128.0,
        key_sync=True,
        transition_hint="custom",
    )
    loaded = load_play_order(po_conn, "pl", "x")
    e = loaded.entries[0]
    assert e.target_key == "8A"
    assert e.target_tempo == 128.0
    assert e.key_sync is True
    assert e.transition_hint == "custom"


def test_set_override_none_is_write(po_conn: sqlite3.Connection) -> None:
    po_id = _seed_order(po_conn)
    set_override(po_conn, po_id, 0, target_key="8A")
    set_override(po_conn, po_id, 0, target_key=None)
    loaded = load_play_order(po_conn, "pl", "x")
    assert loaded.entries[0].target_key is None


def test_set_override_rejects_invalid_key(po_conn: sqlite3.Connection) -> None:
    po_id = _seed_order(po_conn)
    with pytest.raises(ValueError):
        set_override(po_conn, po_id, 0, target_key="not-a-key")


def test_set_override_rejects_invalid_tempo(po_conn: sqlite3.Connection) -> None:
    po_id = _seed_order(po_conn)
    with pytest.raises(ValueError):
        set_override(po_conn, po_id, 0, target_tempo=-5.0)


def test_set_override_rejects_empty(po_conn: sqlite3.Connection) -> None:
    po_id = _seed_order(po_conn)
    with pytest.raises(ValueError, match="at least one"):
        set_override(po_conn, po_id, 0)


def test_set_override_unknown_position(po_conn: sqlite3.Connection) -> None:
    po_id = _seed_order(po_conn)
    with pytest.raises(LookupError):
        set_override(po_conn, po_id, 999, target_key="8A")


def test_clear_override_resets_all(po_conn: sqlite3.Connection) -> None:
    po_id = _seed_order(po_conn)
    set_override(
        po_conn,
        po_id,
        0,
        target_key="8A",
        target_tempo=120.0,
        key_sync=True,
        transition_hint="hint",
    )
    clear_override(po_conn, po_id, 0)
    loaded = load_play_order(po_conn, "pl", "x")
    e = loaded.entries[0]
    assert e.target_key is None
    assert e.target_tempo is None
    assert e.key_sync is None
    assert e.transition_hint is None
