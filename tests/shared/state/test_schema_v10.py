"""Migration v10: the ``lyric_verdict`` row and the widened ``asset_kind``.

Contract under test: ``specs/karaoke-lyrics-operational-plan.md`` D13.1 and
the ``sync_policies`` half of D13.2, implemented in
:mod:`apps.shared.state.migrations_v10`.

Acceptance criteria, one assertion block each:
- if a fresh ladder does not create ``lyric_verdict`` AND
  ``idx_lyric_verdict_red``, the triage listing silently table-scans and the
  row this whole workstream hangs off does not exist -- broken.
- if the ``verdict`` / ``override`` CHECKs and the Python mirrors
  ``schema.LYRIC_VERDICTS`` / ``schema.LYRIC_OVERRIDES`` disagree, code
  validates against one vocabulary and the DB enforces another -- broken.
- if the sync trio is not ``updated_at TEXT NOT NULL`` + nullable
  ``origin_device_id`` / ``deleted_at``, a peer's NULL-stamped row is stored
  as epoch-old instead of being refused -- broken.
- if a v8 DB with ``sync_policies`` rows migrates to the top and loses a row, a
  tombstone, its PK, or changes its column list, the rebuild moved the sync
  digest and every peer diverges -- broken.
- if a migrated ladder and a fresh ladder do not produce identical normalised
  DDL, two machines are not running the same schema -- broken.
- if a DB carrying a stale ``idx_lyric_verdict_red`` on a renamed-aside table
  migrates QUIETLY, the new table ships without its triage index and nothing
  says so -- broken.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.engine_core.store.schema import normalize_object_sql
from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.shared.state.migrations_v10 import ASSET_KIND_CHECK_VALUES

pytestmark = pytest.mark.requirement("INFRA-01")

_TS = "2026-09-09T12:00:00+00:00"
_SID = "a" * 40
_HASH = "b" * 64

# The v8 asset kinds, i.e. what sync_policies accepted BEFORE this migration.
# Spelled out rather than sliced off ASSET_KIND_CHECK_VALUES so a reordering
# of that tuple cannot quietly redefine what "already existed" means.
_V8_ASSET_KINDS: tuple[str, ...] = (
    "audio",
    "stem_bundle",
    "anlz_cache",
    "vocal_cache",
)


# ----- helpers -------------------------------------------------------------


def _objects(conn: sqlite3.Connection) -> dict[str, str]:
    """``name -> normalised DDL`` for every real table and index."""
    return {
        str(name): normalize_object_sql(str(sql))
        for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index')"
        ).fetchall()
        if sql is not None and not str(name).startswith("sqlite_")
    }


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _seed_v8_db(path: Path) -> None:
    """A DB stamped at v8 with sync_policies rows, one of them a tombstone.

    Built by running the real ladder and then rolling the stamp back, which
    is the only honest way to get a v8 file: hand-writing the v8 DDL here
    would test this module's transcription of it, not the ladder's.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        original = state_schema.SCHEMA_VERSION
        state_schema.SCHEMA_VERSION = 8
        try:
            state_schema.apply_migrations(conn)
        finally:
            state_schema.SCHEMA_VERSION = original
        conn.execute(
            "INSERT INTO machines(machine_id, name, platform, is_hub, "
            "first_seen, last_seen) VALUES ('m1', 'silver', 'macos', 0, ?, ?)",
            (_TS, _TS),
        )
        for index, kind in enumerate(_V8_ASSET_KINDS):
            conn.execute(
                "INSERT INTO sync_policies(machine_id, asset_kind, mode, "
                "cache_budget_mb, updated_at, origin_device_id, deleted_at) "
                "VALUES ('m1', ?, 'pinned', ?, ?, 'm1', ?)",
                (kind, 100 + index, _TS, _TS if kind == "vocal_cache" else None),
            )
        conn.commit()
    finally:
        conn.close()


def _insert_verdict(conn: sqlite3.Connection, **overrides: object) -> None:
    values: dict[str, object] = {
        "stable_id": _SID,
        "verdict": "vocal",
        "override": None,
        "pipeline_version": "2026.09.09-round3a",
        "words_content_hash": _HASH,
        "computed_at": _TS,
        "updated_at": _TS,
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    conn.execute(
        f"INSERT INTO lyric_verdict({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(stable_id) DO UPDATE SET verdict = excluded.verdict, "
        f"override = excluded.override",
        tuple(values.values()),
    )


def _insert_track(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, created_at, updated_at) "
        "VALUES (?, 'isrc', ?, ?)",
        (_SID, _TS, _TS),
    )


# ----- (1) the fresh ladder ------------------------------------------------


def test_fresh_ladder_creates_the_table_and_its_triage_index(
    state_conn: sqlite3.Connection,
) -> None:
    objects = _objects(state_conn)
    assert "lyric_verdict" in objects
    assert "idx_lyric_verdict_red" in objects, (
        "the triage listing sorts on pct_witness_red DESC; without this index "
        "every open of the Lyrics page table-scans the library"
    )
    assert "lyric_verdict" in state_schema.TABLES
    assert _columns(state_conn, "lyric_verdict") == [
        "stable_id",
        "verdict",
        "coverage_pct",
        "source",
        "language_iso3",
        "n_words",
        "n_lines",
        "pct_witness_red",
        "override",
        "override_note",
        "pipeline_version",
        "words_content_hash",
        "computed_at",
        "updated_at",
        "origin_device_id",
        "deleted_at",
    ]


def test_the_sync_trio_is_declared_exactly_as_tracks_declares_it(
    state_conn: sqlite3.Connection,
) -> None:
    """updated_at NOT NULL, the other two nullable -- ADR 04 c7 / D13.1."""
    notnull = {
        str(row[1]): bool(row[3])
        for row in state_conn.execute("PRAGMA table_info(lyric_verdict)")
    }
    assert notnull["updated_at"] is True, (
        "a NULL-stamped verdict row would sync as epoch-old and lose every "
        "conflict silently; NOT NULL makes the peer's batch fail instead"
    )
    assert notnull["origin_device_id"] is False
    assert notnull["deleted_at"] is False
    assert notnull["verdict"] is True
    assert notnull["pipeline_version"] is True
    assert notnull["computed_at"] is True


def test_the_foreign_key_to_tracks_is_enforced(
    state_conn: sqlite3.Connection,
) -> None:
    fks = state_conn.execute("PRAGMA foreign_key_list(lyric_verdict)").fetchall()
    assert [(row[2], row[3], row[4], row[6]) for row in fks] == [
        ("tracks", "stable_id", "stable_id", "CASCADE")
    ]
    with pytest.raises(sqlite3.IntegrityError):
        _insert_verdict(state_conn)


# ----- (2) the CHECK enums and their Python mirrors ------------------------


def test_every_declared_verdict_is_accepted(state_conn: sqlite3.Connection) -> None:
    _insert_track(state_conn)
    for verdict in state_schema.LYRIC_VERDICTS:
        _insert_verdict(state_conn, verdict=verdict)
    stored = state_conn.execute(
        "SELECT verdict FROM lyric_verdict WHERE stable_id = ?", (_SID,)
    ).fetchone()[0]
    assert stored == state_schema.LYRIC_VERDICTS[-1]


def test_every_declared_override_is_accepted(state_conn: sqlite3.Connection) -> None:
    _insert_track(state_conn)
    for override in (None, *state_schema.LYRIC_OVERRIDES):
        _insert_verdict(state_conn, override=override)


def test_an_undeclared_verdict_is_refused(state_conn: sqlite3.Connection) -> None:
    _insert_track(state_conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_verdict(state_conn, verdict="instrumental")


def test_unknown_is_not_an_override(state_conn: sqlite3.Connection) -> None:
    """'unknown' is a computed verdict, never a human one (D13.1)."""
    _insert_track(state_conn)
    assert "unknown" in state_schema.LYRIC_VERDICTS
    assert "unknown" not in state_schema.LYRIC_OVERRIDES
    with pytest.raises(sqlite3.IntegrityError):
        _insert_verdict(state_conn, override="unknown")


def test_a_words_hash_that_is_not_a_sha256_is_refused(
    state_conn: sqlite3.Connection,
) -> None:
    _insert_track(state_conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_verdict(state_conn, words_content_hash="deadbeef")
    _insert_verdict(state_conn, words_content_hash=None)


# ----- (3) the sync_policies rebuild --------------------------------------


def test_the_widened_check_admits_the_two_new_kinds(
    state_conn: sqlite3.Connection,
) -> None:
    state_conn.execute(
        "INSERT INTO machines(machine_id, name, platform, is_hub, first_seen, "
        "last_seen) VALUES ('m1', 'silver', 'macos', 0, ?, ?)",
        (_TS, _TS),
    )
    for kind in ASSET_KIND_CHECK_VALUES:
        state_conn.execute(
            "INSERT INTO sync_policies(machine_id, asset_kind, mode) "
            "VALUES ('m1', ?, 'pinned')",
            (kind,),
        )
    assert state_conn.execute(
        "SELECT COUNT(*) FROM sync_policies"
    ).fetchone()[0] == len(ASSET_KIND_CHECK_VALUES)
    with pytest.raises(sqlite3.IntegrityError):
        state_conn.execute(
            "INSERT INTO sync_policies(machine_id, asset_kind, mode) "
            "VALUES ('m1', 'beatgrid_cache', 'pinned')"
        )


def test_a_v8_db_migrates_with_its_policy_rows_and_column_list_intact(
    tmp_path: Path,
) -> None:
    """The rebuild must move nothing the sync digest can see.

    ``protocol.table_digest`` hashes the column NAMES and ORDER out of
    ``PRAGMA table_info`` and never sees CHECK text, so a v8 peer and a v10
    peer only stay converged if the rebuild preserves both -- along with
    every row, including the tombstones.
    """
    db_path = tmp_path / "state" / "state.db"
    _seed_v8_db(db_path)

    before = sqlite3.connect(db_path)
    try:
        v8_columns = _columns(before, "sync_policies")
        v8_rows = before.execute(
            "SELECT machine_id, asset_kind, mode, cache_budget_mb, updated_at, "
            "origin_device_id, deleted_at FROM sync_policies ORDER BY asset_kind"
        ).fetchall()
    finally:
        before.close()
    assert len(v8_rows) == len(_V8_ASSET_KINDS), "fixture seeded nothing"

    conn = state_db.open_rw(db_path)
    try:
        assert conn.execute(
            "SELECT MAX(version) FROM schema_meta"
        ).fetchone()[0] == state_schema.SCHEMA_VERSION
        assert _columns(conn, "sync_policies") == v8_columns
        assert conn.execute(
            "SELECT machine_id, asset_kind, mode, cache_budget_mb, updated_at, "
            "origin_device_id, deleted_at FROM sync_policies ORDER BY asset_kind"
        ).fetchall() == v8_rows
        tombstoned = conn.execute(
            "SELECT COUNT(*) FROM sync_policies WHERE deleted_at IS NOT NULL"
        ).fetchone()[0]
        assert tombstoned == 1, (
            "the rebuild dropped a tombstone; this machine's digest now "
            "disagrees with every peer that still remembers the delete"
        )
        pk = [
            str(row[1])
            for row in conn.execute("PRAGMA table_info(sync_policies)")
            if int(row[5]) > 0
        ]
        assert pk == ["machine_id", "asset_kind"]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert "sync_policies_v10" not in _objects(conn), (
            "the scratch table survived the rename"
        )
    finally:
        conn.close()


def test_a_migrated_ladder_and_a_fresh_ladder_agree_byte_for_byte(
    tmp_path: Path,
) -> None:
    migrated_path = tmp_path / "migrated" / "state" / "state.db"
    _seed_v8_db(migrated_path)
    migrated = state_db.open_rw(migrated_path)
    fresh = state_db.open_rw(tmp_path / "fresh" / "state" / "state.db")
    try:
        assert _objects(migrated) == _objects(fresh), (
            "a machine that upgraded and a machine that started fresh are "
            "not running the same schema"
        )
    finally:
        migrated.close()
        fresh.close()


# ----- (4) the stale-index trap the bare CREATE INDEX exists for -----------


def test_a_stale_index_name_on_a_renamed_aside_table_fails_the_migration(
    tmp_path: Path,
) -> None:
    """D13.6's rename-aside step must drop the branch-era indexes first.

    SQLite carries an index with its table through ``ALTER TABLE ... RENAME
    TO``, keeping the index's ORIGINAL name. With ``CREATE INDEX IF NOT
    EXISTS`` this migration would then be a silent no-op and this Mac's
    506-track library would land without its triage index and no error. The
    bare ``CREATE INDEX`` turns that into a loud failure.
    """
    db_path = tmp_path / "stale" / "state" / "state.db"
    _seed_v8_db(db_path)

    conn = sqlite3.connect(db_path)
    try:
        # The branch-era pair, verbatim in shape, then renamed aside exactly
        # as the runbook does -- but WITHOUT the DROP INDEX step.
        conn.execute(
            "CREATE TABLE lyric_verdict (stable_id TEXT PRIMARY KEY, "
            "pct_witness_red REAL)"
        )
        conn.execute(
            "CREATE INDEX idx_lyric_verdict_red "
            "ON lyric_verdict(pct_witness_red DESC)"
        )
        conn.execute("ALTER TABLE lyric_verdict RENAME TO lyric_verdict_legacy")
        conn.commit()
        assert "idx_lyric_verdict_red" in _objects(conn), (
            "fixture assumption: the rename carries the index across"
        )
        with pytest.raises(sqlite3.OperationalError, match="idx_lyric_verdict_red"):
            state_schema.apply_migrations(conn)
        # Every rung below v10 (v9 enrollment included) succeeds and stamps;
        # only the lyric_verdict rung fails, so the DB sits one below the top.
        assert conn.execute(
            "SELECT MAX(version) FROM schema_meta"
        ).fetchone()[0] == state_schema.SCHEMA_VERSION - 1, (
            "a failed step must not stamp its version"
        )
    finally:
        conn.close()
