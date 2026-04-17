"""INFRA-01 schema + migrations tests."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema


pytestmark = pytest.mark.requirement("INFRA-01")


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def test_fresh_init_creates_all_tables(state_conn: sqlite3.Connection) -> None:
    names = _tables(state_conn)
    for expected in state_schema.TABLES:
        assert expected in names, (
            f"expected table {expected!r} after fresh init; got {sorted(names)}"
        )
    assert "schema_meta" in names


def test_migrations_idempotent(state_db_path: Path) -> None:
    conn1 = state_db.open_rw(state_db_path)
    try:
        v1 = state_schema.apply_migrations(conn1)
    finally:
        conn1.close()
    conn2 = state_db.open_rw(state_db_path)
    try:
        v2 = state_schema.apply_migrations(conn2)
        row = conn2.execute("SELECT COUNT(*) FROM schema_meta").fetchone()
    finally:
        conn2.close()
    assert v1 == v2 == state_schema.SCHEMA_VERSION
    assert row[0] == state_schema.SCHEMA_VERSION


def test_pragmas_set(state_conn: sqlite3.Connection) -> None:
    journal = state_conn.execute("PRAGMA journal_mode").fetchone()[0]
    fk = state_conn.execute("PRAGMA foreign_keys").fetchone()[0]
    busy = state_conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert str(journal).lower() == "wal"
    assert int(fk) == 1
    assert int(busy) == 5000


def test_open_ro_rejects_writes(state_db_path: Path) -> None:
    state_db.open_rw(state_db_path).close()
    conn = state_db.open_ro(state_db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO events(ts, kind, payload_json) VALUES (?,?,?)",
                ("2026-01-01T00:00:00Z", "track.upsert", "{}"),
            )
    finally:
        conn.close()


def test_open_ro_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        state_db.open_ro(tmp_path / "nope.db")


def test_schema_meta_records_version(state_conn: sqlite3.Connection) -> None:
    row = state_conn.execute(
        "SELECT version, applied_at FROM schema_meta WHERE version = ?",
        (state_schema.SCHEMA_VERSION,),
    ).fetchone()
    assert row is not None
    assert row[0] == state_schema.SCHEMA_VERSION
    assert row[1].endswith("+00:00") or row[1].endswith("Z")


def test_tracks_tier_check_constraint(state_conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        state_conn.execute(
            "INSERT INTO tracks(stable_id, stable_id_tier, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("x" * 40, "BAD", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )


def test_track_fields_source_check_constraint(state_conn: sqlite3.Connection) -> None:
    state_conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        ("a" * 40, "isrc", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        state_conn.execute(
            "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
            "modified_at) VALUES (?, ?, ?, ?, ?)",
            ("a" * 40, "bpm", "128", "NOPE", "2026-01-01T00:00:00Z"),
        )


def test_track_fields_confidence_bounds(state_conn: sqlite3.Connection) -> None:
    state_conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        ("b" * 40, "isrc", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        state_conn.execute(
            "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
            "confidence, modified_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("b" * 40, "bpm", "128", "rekordbox", 2.0, "2026-01-01T00:00:00Z"),
        )
