"""Phase 08 table smoke tests.

Confirms :func:`ensure_phase08_tables` creates ``pairings`` + ``smartlists``
on a fresh DB, is idempotent, and enforces CHECK constraints.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.pairings import ensure_phase08_tables


pytestmark = pytest.mark.requirement("CAT-03")


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def test_creates_pairings_and_smartlists(state_conn: sqlite3.Connection) -> None:
    names = _tables(state_conn)
    assert "pairings" in names
    assert "smartlists" in names


def test_ensure_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "s.db"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        ensure_phase08_tables(conn)
        ensure_phase08_tables(conn)
        ensure_phase08_tables(conn)
        # Still only two tables, no dup indexes, no errors.
        names = _tables(conn)
        assert "pairings" in names and "smartlists" in names
    finally:
        conn.close()


def test_pairings_direction_check(state_conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        state_conn.execute(
            "INSERT INTO pairings (from_stable_id, to_stable_id, direction, "
            "source, created_at, modified_at) VALUES (?,?,?,?,?,?)",
            ("a", "b", "sideways", "manual", "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:00+00:00"),
        )


def test_pairings_source_check(state_conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        state_conn.execute(
            "INSERT INTO pairings (from_stable_id, to_stable_id, direction, "
            "source, created_at, modified_at) VALUES (?,?,?,?,?,?)",
            ("a", "b", "into", "bogus", "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:00+00:00"),
        )


def test_pairings_confidence_bounds(state_conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        state_conn.execute(
            "INSERT INTO pairings (from_stable_id, to_stable_id, direction, "
            "source, confidence, created_at, modified_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("a", "b", "into", "manual", 2.0,
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )


def test_smartlists_unique_name(state_conn: sqlite3.Connection) -> None:
    state_conn.execute(
        "INSERT INTO smartlists (id, name, rule, referenced_fields, "
        "created_at, modified_at) VALUES (?,?,?,?,?,?)",
        ("id1", "dupe", "{}", "[]",
         "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        state_conn.execute(
            "INSERT INTO smartlists (id, name, rule, referenced_fields, "
            "created_at, modified_at) VALUES (?,?,?,?,?,?)",
            ("id2", "dupe", "{}", "[]",
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
