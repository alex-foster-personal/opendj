"""The guard that makes "same WIRE_VERSION" MEAN "same synced rows".

The hub and spoke gate on WIRE_VERSION alone (:mod:`apps.sync_hub.wire_version`),
so the claim a same-wire peer on another schema can sync rests entirely on
this: a synced column, constraint or table cannot change without the wire
version changing with it. Each test below attacks one half of that claim on
a real DB built by the real ladder.

Regression lines:
  - if a synced column is added with no WIRE_VERSION bump and the guard is silent then broken
  - if a machine-local table change trips the guard then broken
  - if a widened CHECK on a synced table reads as the same shape then broken
  - if WIRE_FINGERPRINTS can skip or repeat a version then broken

[if] a synced column moves without a wire bump unnoticed [then] fail, [else stop].
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.sync_hub import protocol, wire_version

pytestmark = pytest.mark.requirement("CAT-04")


@pytest.fixture
def fresh(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A state DB opened the way production opens one: real ladder, real file."""
    path = tmp_path / "state" / "state.db"
    path.parent.mkdir(parents=True)
    conn = state_db.open_rw(path)
    try:
        yield conn
    finally:
        conn.close()


def test_this_trees_synced_shape_is_the_one_its_wire_version_pins(
    fresh: sqlite3.Connection,
) -> None:
    """If the ladder's synced shape differs from WIRE_FINGERPRINTS[WIRE_VERSION] then broken."""
    shape = wire_version.measure_wire_shape(fresh)
    assert set(shape["tables"]) == set(protocol.DIGEST_TABLES), "measured every synced table"
    assert all(facts["columns"] for facts in shape["tables"].values())
    assert wire_version.wire_shape_drift(fresh) is None
    assert wire_version.fresh_ladder_drift() is None, "the lint's subject agrees"


def test_adding_a_synced_column_without_a_wire_bump_is_caught(
    fresh: sqlite3.Connection,
) -> None:
    """If a new tracks column passes the guard without a WIRE_VERSION bump then broken."""
    fresh.execute("ALTER TABLE tracks ADD COLUMN wire_probe TEXT")

    drift = wire_version.wire_shape_drift(fresh)

    assert drift is not None
    assert f"WIRE_VERSION = {wire_version.WIRE_VERSION + 1}" in drift, "names the fix"
    assert "wire_probe" in drift, "and shows what moved"


def test_a_machine_local_table_is_not_a_wire_change(fresh: sqlite3.Connection) -> None:
    """If a table outside the sync set trips the guard then broken: every step is a flag day."""
    fresh.execute("CREATE TABLE local_probe (id INTEGER PRIMARY KEY, note TEXT NOT NULL)")
    fresh.execute("CREATE INDEX idx_tracks_probe ON tracks(title)")

    assert wire_version.wire_shape_drift(fresh) is None


def test_a_widened_check_on_a_synced_table_is_a_different_shape() -> None:
    """If a CHECK widening, the v10 sync_policies shape, reads as the same wire then broken."""
    narrow = "CREATE TABLE t (k TEXT CHECK (k IN ('a', 'b')))"
    wide = "CREATE TABLE t (k TEXT CHECK (k IN ('a', 'b', 'c')))"
    relaid = "CREATE TABLE t (\n  k TEXT  CHECK(k IN ('a','b'))  -- CHECK (not me)\n)"
    quoted = "CREATE TABLE t (k TEXT CHECK (k != ')'), n INT CHECK (n > 0))"

    assert wire_version.check_clauses(narrow) != wire_version.check_clauses(wide)
    assert wire_version.check_clauses(narrow) == wire_version.check_clauses(relaid), (
        "layout and comments are not wire changes"
    )
    assert wire_version.check_clauses(quoted) == ("k != ')'", "n > 0")
    with pytest.raises(wire_version.WireShapeError):
        wire_version.check_clauses("CREATE TABLE t (k TEXT CHECK (k IN ('a')")


def test_the_fingerprint_history_has_one_entry_per_wire_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If WIRE_FINGERPRINTS may skip or repeat a version then broken."""
    assert wire_version.fingerprint_history_problems() == []

    monkeypatch.setattr(wire_version, "WIRE_FINGERPRINTS", {1: "aa", 3: "bb"})
    monkeypatch.setattr(wire_version, "WIRE_VERSION", 3)
    assert any("no gaps" in p for p in wire_version.fingerprint_history_problems())

    monkeypatch.setattr(wire_version, "WIRE_FINGERPRINTS", {1: "aa", 2: "aa"})
    monkeypatch.setattr(wire_version, "WIRE_VERSION", 2)
    assert any("same fingerprint" in p for p in wire_version.fingerprint_history_problems())


def test_wire_v5_pins_the_legacy_track_fields_semantic_contract(
    fresh: sqlite3.Connection,
) -> None:
    """If wire v5 omits the track_fields modified_at fallback contract then broken."""
    shape = wire_version.measure_wire_shape(fresh)
    assert shape.get("semantic_contract") == (
        wire_version.WIRE_SEMANTIC_CONTRACTS[wire_version.WIRE_VERSION]
    )
    assert wire_version.WIRE_FINGERPRINTS[wire_version.WIRE_VERSION] == (
        wire_version.wire_fingerprint(shape)
    )


def test_a_v4_peer_fingerprint_differs_from_wire_v5() -> None:
    """If wire v5 reuses v4's fingerprint then broken: semantic contract changed."""
    assert wire_version.WIRE_FINGERPRINTS[4] != wire_version.WIRE_FINGERPRINTS[5]
    assert wire_version.WIRE_SEMANTIC_CONTRACTS.get(4) is None
    assert wire_version.WIRE_SEMANTIC_CONTRACTS[5] == (
        "track_fields_modified_at_lww_fallback"
    )


def test_a_missing_synced_table_is_unknown_not_a_verdict() -> None:
    """If an empty DB yields a fingerprint instead of an error then broken."""
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(wire_version.WireShapeError):
            wire_version.measure_wire_shape(conn)
    finally:
        conn.close()
