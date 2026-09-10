"""Migration v7 (CLOUDSYNC sync-safe schema) + machine identity.

Contract under test: specs/design_decision_05.md. CLOUDSYNC yielded v6 to the
Google-auth branch (users + auth_sessions) when it merged to main first, so
this schema now lands as migration v7, one step above auth's v6. The DDL is
unchanged; only its position in the ladder moved.
Acceptance criteria, one assertion block each:
- if a fresh DB opened via ``db.open_rw`` does not land on the ladder's
  terminal version with ``PRAGMA foreign_keys`` ON, sync has no schema to
  run against -- broken. (The number in that assertion moves with the
  ladder: v7 when this file was written, v9 today.)
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
import uuid
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


def _index_columns(conn: sqlite3.Connection, index: str) -> list[str]:
    return [row[2] for row in conn.execute(f"PRAGMA index_info({index})")]


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


def test_fresh_db_reaches_v9_with_foreign_keys_on(state_db_path: Path) -> None:
    conn = state_db.open_rw(state_db_path)
    try:
        assert state_schema.SCHEMA_VERSION == 9
        assert len(state_schema.MIGRATIONS) == state_schema.SCHEMA_VERSION
        version = conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
        assert version == 9
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        tables = _user_tables(conn)
        for expected in state_schema.TABLES:
            assert expected in tables, f"the migration ladder did not create {expected!r}"
    finally:
        conn.close()


def test_v7_adds_sync_columns_to_every_synced_table(
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


# ----- (2) v5 -> v7 round-trip on real rows --------------------------------


def test_v5_track_locations_migrate_with_minted_text_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "v5.db"
    seeded = _seed_v5_db(db_path, locations=7)

    conn = state_db.open_rw(db_path)
    try:
        # v9, not v7: this fixture seeds a v5 DB and opens it through
        # open_rw, which migrates all the way to the CURRENT SCHEMA_VERSION
        # (8 after the af--analysis-retention migration landed as v8, then 9
        # after the karaoke lyric_verdict migration), not to a version number
        # frozen when this test was written.
        assert conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] == 9

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

        # Both FKs survived the drop-and-rename, and are enforced.
        fks = conn.execute("PRAGMA foreign_key_list(track_locations)").fetchall()
        assert sorted((row[2], row[3], row[4]) for row in fks) == [
            ("machines", "machine_id", "machine_id"),
            ("tracks", "stable_id", "stable_id"),
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO track_locations(stable_id, kind, file_path, "
                "created_at, updated_at) VALUES ('ghost', 'local', '/x.flac', ?, ?)",
                (_TS, _TS),
            )

        # All three indexes are back, and the partial UNIQUE ones still bite
        # for one machine writing the same logical row twice.
        assert _indexes(conn, "track_locations") >= {
            "idx_track_locations_stable",
            "idx_track_locations_path",
            "idx_track_locations_url",
        }
        local_machine = mid.get_or_create_machine_id(db_path.parent)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO track_locations(stable_id, machine_id, kind, "
                "file_path, created_at, updated_at) "
                "VALUES (?, ?, 'local', ?, ?, ?)",
                (f"{0:040d}", local_machine, seeded[0], _TS, _TS),
            )
    finally:
        conn.close()


def test_v7_indexes_lead_with_stable_id_then_machine_id(
    state_conn: sqlite3.Connection,
) -> None:
    """ADR 08 point 1: machine_id joins the logical identity of a location."""
    assert "machine_id" in _columns(state_conn, "track_locations")
    assert _index_columns(state_conn, "idx_track_locations_path") == [
        "stable_id",
        "machine_id",
        "kind",
        "file_path",
    ]
    assert _index_columns(state_conn, "idx_track_locations_url") == [
        "stable_id",
        "machine_id",
        "kind",
        "remote_url",
    ]


def test_two_machines_hold_the_same_path_without_colliding(
    state_conn: sqlite3.Connection,
) -> None:
    """Regression for round 1 finding 1 (critical).

    Two machines that both migrated a v4-seeded location row mint different
    ``location_id`` values for the same logical row. Before machine_id joined
    the unique index that pair violated
    ``UNIQUE(stable_id, kind, file_path)``, the hub returned 409, the whole
    push rolled back, and the second spoke could never sync again.
    """
    state_conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, created_at, updated_at) "
        "VALUES ('trk-1', 'isrc', ?, ?)",
        (_TS, _TS),
    )
    for machine_id, name in (("m-air", "air"), ("m-silver", "silver")):
        state_conn.execute(
            "INSERT INTO machines(machine_id, name, platform, is_hub, "
            "first_seen, last_seen) VALUES (?, ?, 'macos', 0, ?, ?)",
            (machine_id, name, _TS, _TS),
        )
        state_conn.execute(
            "INSERT INTO track_locations(location_id, stable_id, machine_id, "
            "kind, file_path, created_at, updated_at) "
            "VALUES (?, 'trk-1', ?, 'local', '/Music/a.mp3', ?, ?)",
            (uuid.uuid4().hex, machine_id, _TS, _TS),
        )
    rows = state_conn.execute(
        "SELECT machine_id FROM track_locations WHERE stable_id = 'trk-1' "
        "ORDER BY machine_id"
    ).fetchall()
    assert [row[0] for row in rows] == ["m-air", "m-silver"]

    # And the same machine still cannot hold the row twice.
    with pytest.raises(sqlite3.IntegrityError):
        state_conn.execute(
            "INSERT INTO track_locations(location_id, stable_id, machine_id, "
            "kind, file_path, created_at, updated_at) "
            "VALUES (?, 'trk-1', 'm-air', 'local', '/Music/a.mp3', ?, ?)",
            (uuid.uuid4().hex, _TS, _TS),
        )


def test_local_changelog_mirrors_hub_changelog(
    state_conn: sqlite3.Connection,
) -> None:
    """ADR 08 point 3: the spoke-side fence needs its own monotonic seq."""
    assert "local_changelog" in state_schema.TABLES
    assert _columns(state_conn, "local_changelog") == _columns(
        state_conn, "hub_changelog"
    )
    for row_pk in ('["a"]', '["b"]'):
        state_conn.execute(
            "INSERT INTO local_changelog(table_name, row_pk, updated_at, "
            "origin_device_id, received_at) VALUES ('tracks', ?, ?, 'dev', ?)",
            (row_pk, _TS, _TS),
        )
    seqs = [
        row[0]
        for row in state_conn.execute("SELECT seq FROM local_changelog ORDER BY seq")
    ]
    assert seqs == [1, 2], "local_changelog seq must be monotonic"


def test_v5_locations_are_claimed_by_this_machine_on_open(tmp_path: Path) -> None:
    """The post-migration hook backfills what pure SQL could not know."""
    db_path = tmp_path / "claimed.db"
    seeded = _seed_v5_db(db_path, locations=3)

    conn = state_db.open_rw(db_path)
    try:
        expected = mid.get_or_create_machine_id(tmp_path)
        rows = conn.execute(
            "SELECT machine_id, origin_device_id FROM track_locations"
        ).fetchall()
        assert len(rows) == len(seeded)
        assert {row[0] for row in rows} == {expected}
        assert {row[1] for row in rows} == {expected}, (
            "a claimed row with no origin loses every LWW tiebreak"
        )
        # The machines row the FK points at exists, and the claim is offerable.
        assert conn.execute(
            "SELECT COUNT(*) FROM machines WHERE machine_id = ?", (expected,)
        ).fetchone()[0] == 1
        logged = conn.execute(
            "SELECT COUNT(*) FROM local_changelog WHERE table_name = ?",
            ("track_locations",),
        ).fetchone()[0]
        assert logged == len(seeded)
    finally:
        conn.close()

    # Re-opening claims nothing further: the hook is idempotent.
    conn = state_db.open_rw(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM local_changelog"
        ).fetchone()[0] == len(seeded)
    finally:
        conn.close()


def test_opening_a_fresh_db_mints_no_identity(state_db_path: Path) -> None:
    """A DB with nothing to claim must not write an id file or a machines row.

    Guards the blast radius of the post-migration hook: ``open_rw`` is called
    by most of this repo, and an unconditional register would put a hostname
    row in every test DB.
    """
    conn = state_db.open_rw(state_db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM local_changelog").fetchone()[0] == 0
    finally:
        conn.close()
    assert not mid.machine_id_path(state_db_path.parent).exists()


def test_insert_without_location_id_mints_one(state_conn: sqlite3.Connection) -> None:
    """A writer that predates the sync migration must not create NULL-keyed,
    unsyncable rows."""
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


def test_v5_to_v9_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "twice.db"
    _seed_v5_db(db_path, locations=3)
    state_db.open_rw(db_path).close()
    conn = state_db.open_rw(db_path)
    try:
        assert state_schema.apply_migrations(conn) == 9
        assert conn.execute("SELECT COUNT(*) FROM track_locations").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0] == 9
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
