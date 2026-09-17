"""LIBMX-02: smartlist tombstone delete on unmigrated tables."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.pairings.schema_sql import ensure_phase08_tables
from tests.test_smartlists_route import (
    _BPM_RULE,
    _make_client,
)

pytest_plugins = ["tests.test_smartlists_route"]


_OLD_SMARTLISTS_DDL = """
CREATE TABLE smartlists (
    id                           TEXT PRIMARY KEY,
    name                         TEXT NOT NULL UNIQUE,
    rule                         TEXT NOT NULL,
    rule_schema_version          INTEGER NOT NULL DEFAULT 0,
    referenced_fields            TEXT NOT NULL,
    order_by                     TEXT NOT NULL DEFAULT 'added_date desc',
    last_evaluated_at            TEXT,
    last_materialized_track_ids  TEXT,
    created_at                   TEXT NOT NULL,
    modified_at                  TEXT NOT NULL
)
"""


@pytest.mark.requirement("LIBMX-02")
def test_delete_on_unmigrated_table_tombstones(tmp_path: Path):
    """[if] smartlists lacks deleted_at [then] DELETE migrates and tombstones, [else stop]."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_OLD_SMARTLISTS_DDL)
        conn.execute(
            "INSERT INTO smartlists (id, name, rule, referenced_fields, "
            "created_at, modified_at) VALUES (?,?,?,?,?,?)",
            (
                "sl-legacy",
                "legacy-name",
                "{}",
                "[]",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with _make_client(db_path) as client:
        r = client.delete("/api/v1/smartlists/sl-legacy")
        assert r.status_code == 204
        recreated = client.post(
            "/api/v1/smartlists",
            json={"name": "legacy-name", "rule": _BPM_RULE},
        )
        assert recreated.status_code == 201

    conn = sqlite3.connect(str(db_path))
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(smartlists)")
        }
        assert "deleted_at" in columns
        row = conn.execute(
            "SELECT deleted_at, name FROM smartlists WHERE id=?",
            ("sl-legacy",),
        ).fetchone()
        assert row[0] is not None
        assert "__deleted__" in row[1]
    finally:
        conn.close()


@pytest.mark.requirement("LIBMX-02")
def test_ensure_phase08_adds_deleted_at_idempotent(tmp_path: Path):
    """[if] ensure_phase08_tables runs twice [then] deleted_at exists once, [else stop]."""
    db_path = tmp_path / "phase08.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_OLD_SMARTLISTS_DDL)
        ensure_phase08_tables(conn)
        ensure_phase08_tables(conn)
        columns = [
            row[1] for row in conn.execute("PRAGMA table_info(smartlists)")
        ]
        assert columns.count("deleted_at") == 1
    finally:
        conn.close()
