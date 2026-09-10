"""Migration v9 adds ownership WITHOUT moving a single wire byte (ADR 12 D).

The load-bearing test here is :func:`test_the_digest_is_byte_identical_across_v9`.
The whole ownership design -- a link table rather than a column on
``machines`` -- was chosen so that claim could be made, so a test that could
not fail if the claim were false would be the point of the exercise thrown
away.

Regression one-liners:
  - if the enrollment step is not the TOP of the ladder then broken
  - if a v8 database does not migrate to 9 with both tables present then broken
  - if the digest over a library differs before vs after the v9 migration then broken
  - if either new table joins SYNC_TABLES or DIGEST_TABLES then broken
  - if deleting a users row leaves its machine_owners rows behind then broken
  - if either new table has no column docs then opening a DB breaks
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.database.column_docs import COLUMN_DOCS, TABLE_DOCS
from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.shared.state import sync_stamp
from apps.shared.state.migrations_v9 import _V9
from apps.sync_hub import protocol

NEW_TABLES: tuple[str, ...] = ("machine_owners", "enrollment_grants")

#: The rung BELOW the enrollment migration, derived rather than written
#: down. Every literal version number in this repo's schema tests has had
#: to be hand-bumped by whoever landed the next migration, which turns a
#: routine change into a set of failures that name nothing real.
PREVIOUS_VERSION: int = state_schema.SCHEMA_VERSION - 1


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _migrate_to(conn: sqlite3.Connection, version: int) -> None:
    """Run the ladder up to ``version`` only, so the rung BELOW the top
    can be built and the migration under test has something to migrate."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for index in range(version):
        for statement in state_schema.MIGRATIONS[index]:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
            (index + 1, sync_stamp.canonical_now()),
        )


def _seed_library(conn: sqlite3.Connection) -> None:
    """A few real rows in the synced tables, so the digest has something to say."""
    stamp = sync_stamp.canonical_now()
    # The stable_id is a BOUND PARAMETER, computed in Python. Written as
    # `'a' * 40` inside the SQL it was SQLite doing the arithmetic: 'a'
    # coerces to 0, 0 * 40 is 0, and TEXT affinity stored the one character
    # "0" -- so the fixture row was not shaped like a real track at all
    # (Claude review, PR #1648 P3).
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, "
        "created_at, updated_at, origin_device_id) "
        "VALUES (?, 'inferred', 'Digest Fixture', '[\"Someone\"]', ?, ?, 'dev')",
        ("a" * 40, stamp, stamp),
    )
    conn.execute(
        "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, "
        "created_at, updated_at, origin_device_id) "
        "VALUES ('pl-1', 'Fixture', 'rekordbox', 'rb-1', ?, ?, 'dev')",
        (stamp, stamp),
    )


def test_the_enrollment_step_is_the_top_of_the_ladder():
    """An INVARIANT, not the version number.

    A rule that pins a VALUE has to be maintained forever and rots silently
    between maintenances -- this test read ``SCHEMA_VERSION == 8`` on the
    branch this work was re-landed from, and by the time it landed the number
    meant a different migration entirely. What actually matters is that the
    enrollment DDL is the last rung and that the target version counts the
    rungs, and neither can go stale.
    """
    assert state_schema.MIGRATIONS[-1] is _V9
    assert len(state_schema.MIGRATIONS) == state_schema.SCHEMA_VERSION


def test_a_v8_database_migrates_to_nine_with_both_tables(tmp_path: Path):
    path = tmp_path / "v8.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    _migrate_to(conn, PREVIOUS_VERSION)
    assert (
        conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
        == PREVIOUS_VERSION
    )
    for table in NEW_TABLES:
        assert table not in _tables(conn), (
            "the control: these tables must be ABSENT at v8, or the "
            "post-migration assertion proves nothing"
        )
    assert state_schema.apply_migrations(conn) == state_schema.SCHEMA_VERSION
    present = _tables(conn)
    for table in NEW_TABLES:
        assert table in present
    conn.close()


def test_the_digest_is_byte_identical_across_v9(tmp_path: Path):
    """The claim the link-table shape was chosen to be able to make."""
    path = tmp_path / "digest.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate_to(conn, PREVIOUS_VERSION)
    _seed_library(conn)
    before = protocol.sync_digest(conn, seq=0)

    assert state_schema.apply_migrations(conn) == state_schema.SCHEMA_VERSION
    after = protocol.sync_digest(conn, seq=0)
    conn.close()

    assert after.overall == before.overall
    assert after.tables == before.tables


def test_the_digest_notices_a_real_change(tmp_path: Path):
    """The control for the test above: a digest that never moves is not a digest."""
    path = tmp_path / "control.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate_to(conn, PREVIOUS_VERSION)
    _seed_library(conn)
    before = protocol.sync_digest(conn, seq=0)
    conn.execute("UPDATE tracks SET title = 'Moved'")
    after = protocol.sync_digest(conn, seq=0)
    conn.close()
    assert after.overall != before.overall


@pytest.mark.parametrize("table", NEW_TABLES)
def test_the_new_tables_are_outside_the_sync_set(table: str):
    assert table not in protocol.SYNC_TABLES
    assert table not in protocol.DIGEST_TABLES


@pytest.mark.parametrize("table", NEW_TABLES)
def test_the_new_tables_are_declared_and_documented(table: str):
    assert table in state_schema.TABLES
    assert table in TABLE_DOCS
    assert table in COLUMN_DOCS


def test_deleting_a_user_drops_its_ownership_rows(tmp_path: Path):
    path = tmp_path / "cascade.db"
    conn = state_db.open_rw(path)
    try:
        stamp = sync_stamp.canonical_now()
        conn.execute(
            "INSERT INTO users(google_sub, email, created_at, updated_at) "
            "VALUES ('sub-1', 'a@example.com', ?, ?)",
            (stamp, stamp),
        )
        conn.execute(
            "INSERT INTO machines(machine_id, name, platform, is_hub, "
            "data_root, first_seen, last_seen) "
            "VALUES ('m1', 'one', 'linux', 0, '/tmp/one', ?, ?)",
            (stamp, stamp),
        )
        conn.execute(
            "INSERT INTO machine_owners(machine_id, google_sub, hub_machine_id, "
            "enrolled_at, enrolled_via) VALUES ('m1', 'sub-1', 'hub-1', ?, 'grant')",
            (stamp,),
        )
        assert conn.execute("SELECT COUNT(*) FROM machine_owners").fetchone()[0] == 1
        conn.execute("DELETE FROM users WHERE google_sub = 'sub-1'")
        assert conn.execute("SELECT COUNT(*) FROM machine_owners").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0] == 1, (
            "the machine survives its owner being deleted; it reads unowned"
        )
    finally:
        conn.close()


def test_enrolled_via_rejects_an_unknown_provenance(tmp_path: Path):
    path = tmp_path / "check.db"
    conn = state_db.open_rw(path)
    try:
        stamp = sync_stamp.canonical_now()
        conn.execute(
            "INSERT INTO users(google_sub, email, created_at, updated_at) "
            "VALUES ('sub-1', 'a@example.com', ?, ?)",
            (stamp, stamp),
        )
        conn.execute(
            "INSERT INTO machines(machine_id, name, platform, is_hub, "
            "data_root, first_seen, last_seen) "
            "VALUES ('m1', 'one', 'linux', 0, '/tmp/one', ?, ?)",
            (stamp, stamp),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO machine_owners(machine_id, google_sub, "
                "hub_machine_id, enrolled_at, enrolled_via) "
                "VALUES ('m1', 'sub-1', 'hub-1', ?, 'whatever')",
                (stamp,),
            )
    finally:
        conn.close()
