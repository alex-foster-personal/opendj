"""Migration v6 (CLOUDSYNC sync-safe schema) + machine identity.

Contract under test: specs/design_decision_05.md.
Acceptance criteria, one assertion block each:
- if a fresh DB opened via ``db.open_rw`` does not land on v6 with
  ``PRAGMA foreign_keys`` ON, sync has no schema to run against -- broken.
- if a v5 DB with track_locations rows loses a row, or mints a
  location_id that is not 32 lowercase hex, or drops the FK to tracks or
  either partial UNIQUE index, the PK rebuild is unsafe -- broken.
- if a table exists in the DB that no schema authority declares, the
  four-authorities drift is back and nobody notices -- broken.
- if machine identity is not stable across calls, a restored DB can
  impersonate another machine in hub_changelog -- broken.
"""
from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from apps.shared.pairings import schema_sql as pairings_schema
from apps.shared.play_orders import schema as play_orders_schema
from apps.shared.state import db as state_db
from apps.shared.state import machine_identity as mid
from apps.shared.state import schema as state_schema

pytestmark = pytest.mark.requirement("INFRA-01")

_TS = "2026-08-28T12:00:00+00:00"
_SYNC_COLUMNS = ("updated_at", "origin_device_id", "deleted_at")
_SYNCED_TABLES = (
    "tracks",
    "track_vendor_ids",
    "track_fields",
    "playlists",
    "playlist_memberships",
    "track_locations",
)

# Mirrors apps/launcher/scripts/bootstrap_db.py:133 -- that file is a script
# without an ``__init__.py``, so it cannot be imported. Kept verbatim so the
# tripwire sees exactly what the launcher creates in a real state.db.
_LAUNCHER_DDL: tuple[str, ...] = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(
        title, artist, album, genre, key, tags,
        tokenize='unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tracks_frecency (
        stable_id        TEXT PRIMARY KEY,
        plays            INTEGER DEFAULT 0,
        drags            INTEGER DEFAULT 0,
        last_played_at   INTEGER,
        last_dragged_at  INTEGER
    )
    """,
)


# ----- helpers -------------------------------------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (table,),
        )
        if row[0] is not None
    }


def _user_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not row[0].startswith("sqlite_")
    }


def _seed_v5_db(path: Path, *, locations: int) -> list[str]:
    """Build a v5 DB with ``locations`` track_locations rows. Returns paths."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("BEGIN")
        for step in state_schema.MIGRATIONS[:5]:
            for stmt in step:
                conn.execute(stmt)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 6):
            conn.execute(
                "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
                (version, _TS),
            )
        conn.execute("COMMIT")

        seeded: list[str] = []
        for index in range(locations):
            stable_id = f"{index:040d}"
            file_path = f"/Volumes/DJ/{index}.flac"
            conn.execute(
                "INSERT INTO tracks(stable_id, stable_id_tier, created_at, "
                "updated_at) VALUES (?, 'isrc', ?, ?)",
                (stable_id, _TS, _TS),
            )
            conn.execute(
                "INSERT INTO track_locations(stable_id, kind, role, file_path, "
                "available, created_at, updated_at) "
                "VALUES (?, 'local', 'primary', ?, 1, ?, ?)",
                (stable_id, file_path, _TS, _TS),
            )
            seeded.append(file_path)
        assert conn.execute(
            "SELECT COUNT(*) FROM track_locations"
        ).fetchone()[0] == locations
        return seeded
    finally:
        conn.close()


# ----- (1) fresh DB --------------------------------------------------------


def test_fresh_db_reaches_v6_with_foreign_keys_on(state_db_path: Path) -> None:
    conn = state_db.open_rw(state_db_path)
    try:
        assert state_schema.SCHEMA_VERSION == 6
        assert len(state_schema.MIGRATIONS) == state_schema.SCHEMA_VERSION
        version = conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
        assert version == 6
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        tables = _user_tables(conn)
        for expected in state_schema.TABLES:
            assert expected in tables, f"v6 did not create {expected!r}"
    finally:
        conn.close()


def test_v6_adds_sync_columns_to_every_synced_table(
    state_conn: sqlite3.Connection,
) -> None:
    for table in _SYNCED_TABLES:
        columns = _columns(state_conn, table)
        missing = [c for c in _SYNC_COLUMNS if c not in columns]
        assert not missing, f"{table} is missing sync columns {missing}"


def test_hub_changelog_and_sync_state_shape(state_conn: sqlite3.Connection) -> None:
    state_conn.execute(
        "INSERT INTO hub_changelog(table_name, row_pk, updated_at, "
        "origin_device_id, received_at) VALUES ('tracks', 'x', ?, 'dev', ?)",
        (_TS, _TS),
    )
    seq = state_conn.execute("SELECT seq FROM hub_changelog").fetchone()[0]
    assert seq == 1
    state_conn.execute("INSERT INTO sync_state(peer) VALUES ('hub')")
    row = state_conn.execute(
        "SELECT last_push_seq, last_pull_seq, last_sync_at FROM sync_state"
    ).fetchone()
    assert row == (0, 0, None)


def test_sync_policies_reject_unknown_mode(state_conn: sqlite3.Connection) -> None:
    state_conn.execute(
        "INSERT INTO machines(machine_id, name, platform, is_hub, first_seen, "
        "last_seen) VALUES ('m1', 'silver', 'macos', 0, ?, ?)",
        (_TS, _TS),
    )
    with pytest.raises(sqlite3.IntegrityError):
        state_conn.execute(
            "INSERT INTO sync_policies(machine_id, asset_kind, mode) "
            "VALUES ('m1', 'stem_bundle', 'sometimes')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        state_conn.execute(
            "INSERT INTO sync_policies(machine_id, asset_kind, mode) "
            "VALUES ('nope', 'stem_bundle', 'pinned')"
        )


# ----- (2) v5 -> v6 round-trip on real rows --------------------------------


def test_v5_track_locations_migrate_with_minted_text_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "v5.db"
    seeded = _seed_v5_db(db_path, locations=7)

    conn = state_db.open_rw(db_path)
    try:
        assert conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] == 6

        rows = conn.execute(
            "SELECT location_id, stable_id, file_path, kind, role, available, "
            "created_at, updated_at FROM track_locations ORDER BY file_path"
        ).fetchall()
        assert len(rows) == len(seeded), "the PK rebuild lost or duplicated rows"

        keys = [row[0] for row in rows]
        assert len(set(keys)) == len(keys), "minted location_ids collided"
        for key in keys:
            assert len(key) == 32 and all(c in "0123456789abcdef" for c in key), key

        # Every non-key column survived the copy.
        assert sorted(row[2] for row in rows) == sorted(seeded)
        assert {row[3] for row in rows} == {"local"}
        assert {row[4] for row in rows} == {"primary"}
        assert {row[5] for row in rows} == {1}
        assert {row[6] for row in rows} == {_TS}
        assert {row[7] for row in rows} == {_TS}

        # The colliding INTEGER AUTOINCREMENT key is gone (ADR 05 c3).
        assert "id" not in _columns(conn, "track_locations")

        # FK to tracks survived the drop-and-rename, and is enforced.
        fks = conn.execute("PRAGMA foreign_key_list(track_locations)").fetchall()
        assert [(row[2], row[3], row[4]) for row in fks] == [
            ("tracks", "stable_id", "stable_id")
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO track_locations(stable_id, kind, file_path, "
                "created_at, updated_at) VALUES ('ghost', 'local', '/x.flac', ?, ?)",
                (_TS, _TS),
            )

        # All three indexes are back, and the partial UNIQUE ones still bite.
        assert _indexes(conn, "track_locations") >= {
            "idx_track_locations_stable",
            "idx_track_locations_path",
            "idx_track_locations_url",
        }
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO track_locations(stable_id, kind, file_path, "
                "created_at, updated_at) VALUES (?, 'local', ?, ?, ?)",
                (f"{0:040d}", seeded[0], _TS, _TS),
            )
    finally:
        conn.close()


def test_insert_without_location_id_mints_one(state_conn: sqlite3.Connection) -> None:
    """A writer that predates v6 must not create NULL-keyed, unsyncable rows."""
    state_conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, created_at, updated_at) "
        "VALUES ('t1', 'isrc', ?, ?)",
        (_TS, _TS),
    )
    state_conn.execute(
        "INSERT INTO track_locations(stable_id, kind, role, file_path, "
        "created_at, updated_at) VALUES ('t1', 'local', 'primary', '/a.flac', ?, ?)",
        (_TS, _TS),
    )
    key = state_conn.execute("SELECT location_id FROM track_locations").fetchone()[0]
    assert isinstance(key, str) and len(key) == 32


def test_v5_to_v6_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "twice.db"
    _seed_v5_db(db_path, locations=3)
    state_db.open_rw(db_path).close()
    conn = state_db.open_rw(db_path)
    try:
        assert state_schema.apply_migrations(conn) == 6
        assert conn.execute("SELECT COUNT(*) FROM track_locations").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0] == 6
    finally:
        conn.close()


# ----- (3) undeclared-table tripwire ---------------------------------------


def _provision_every_authority(path: Path) -> sqlite3.Connection:
    """A DB carrying all four schema authorities, as a live state.db does."""
    conn = state_db.open_rw(path)
    pairings_schema.ensure_phase08_tables(conn)
    play_orders_schema.apply_play_order_migrations(conn)
    for stmt in _LAUNCHER_DDL:
        conn.execute(stmt)
    return conn


def test_every_table_in_a_fully_provisioned_db_is_declared(
    state_db_path: Path,
) -> None:
    conn = _provision_every_authority(state_db_path)
    try:
        undeclared = sorted(_user_tables(conn) - state_schema.ALL_KNOWN_TABLES)
        assert not undeclared, (
            "undeclared tables in state.db -- add them to schema.TABLES (this "
            f"module owns them) or FOREIGN_AUTHORITY_TABLES: {undeclared}"
        )
        # And nothing declared is missing, so the tuples are not stale either.
        missing = sorted(state_schema.ALL_KNOWN_TABLES - _user_tables(conn))
        assert not missing, f"declared but never created: {missing}"
    finally:
        conn.close()


def test_tripwire_fails_loudly_on_an_undeclared_table(state_db_path: Path) -> None:
    conn = _provision_every_authority(state_db_path)
    try:
        conn.execute("CREATE TABLE rogue_authority_table (x TEXT)")
        assert _user_tables(conn) - state_schema.ALL_KNOWN_TABLES == {
            "rogue_authority_table"
        }
    finally:
        conn.close()


# ----- (4) machine identity ------------------------------------------------


def test_machine_id_is_minted_once_and_reused(tmp_path: Path) -> None:
    data_dir = tmp_path / "state"
    first = mid.get_or_create_machine_id(data_dir)
    second = mid.get_or_create_machine_id(data_dir)
    assert first == second
    assert len(first) == 32 and all(c in "0123456789abcdef" for c in first)

    path = mid.machine_id_path(data_dir)
    assert path.read_text(encoding="utf-8") == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    other = mid.get_or_create_machine_id(tmp_path / "other-root")
    assert other != first, "two data roots must not share an identity"


def test_corrupt_machine_id_file_fails_fast(tmp_path: Path) -> None:
    data_dir = tmp_path / "state"
    data_dir.mkdir()
    mid.machine_id_path(data_dir).write_text("not-a-uuid", encoding="utf-8")
    with pytest.raises(mid.MachineIdentityError):
        mid.get_or_create_machine_id(data_dir)


def test_unwritable_data_dir_fails_fast(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(mid.MachineIdentityError):
            mid.get_or_create_machine_id(locked / "state")
    finally:
        locked.chmod(0o700)


def test_is_hub_env_is_explicit() -> None:
    assert mid.is_hub_from_env({}) is False
    assert mid.is_hub_from_env({mid.IS_HUB_ENV: "0"}) is False
    assert mid.is_hub_from_env({mid.IS_HUB_ENV: "1"}) is True
    with pytest.raises(mid.MachineIdentityError):
        mid.is_hub_from_env({mid.IS_HUB_ENV: "true"})


def test_register_machine_round_trips_and_is_idempotent(
    state_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(mid.IS_HUB_ENV, "1")
    data_dir = tmp_path / "state"

    written = mid.register_machine(
        state_conn, data_dir=data_dir, name="agentbox", now=_TS
    )
    assert written.is_hub is True
    assert written.platform in ("macos", "windows", "linux")
    assert written.data_root == str(data_dir)

    loaded = mid.load_machine(state_conn, written.machine_id)
    assert loaded == written

    later = "2026-08-29T12:00:00+00:00"
    again = mid.register_machine(
        state_conn, data_dir=data_dir, name="agentbox", now=later
    )
    assert again.machine_id == written.machine_id
    rows = state_conn.execute(
        "SELECT COUNT(*), MIN(first_seen), MAX(last_seen) FROM machines"
    ).fetchone()
    assert rows == (1, _TS, later), "re-registering must upsert, not duplicate"

    assert mid.load_machine(state_conn, "never-registered") is None


def test_duplicate_machine_name_is_rejected(
    state_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    mid.register_machine(state_conn, data_dir=tmp_path / "a", name="silver", now=_TS)
    with pytest.raises(sqlite3.IntegrityError):
        mid.register_machine(
            state_conn, data_dir=tmp_path / "b", name="silver", now=_TS
        )
