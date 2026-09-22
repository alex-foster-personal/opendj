"""Migration v15: legacy track_fields stamp backfill (#3101, #3136).

[if] schema v15 is applied [then] NULL updated_at rows copy modified_at, [else stop].
[if] v15 backfill runs [then] active-role changelog rows are appended, [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.shared.state import sync_stamp
from apps.shared.state.migrations_v15 import (
    _V15,
    HUB_CHANGELOG_TABLE,
    MARKER_TABLE,
    TRACK_FIELDS_STAMP_BACKFILL_MARKER,
    active_changelog_table,
    backfill_track_fields_stamps,
    log_stamp_backfill_rows,
)

pytestmark = [pytest.mark.requirement("INFRA-01"), pytest.mark.requirement("CLOUDSYNC-03")]

_T_LEGACY = "2024-04-17T21:53:19.274000+00:00"
_T_STAMPED = "2026-08-30T10:00:00.000000+00:00"


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _seed_v14_track_fields(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, content_hash, "
        "created_at, updated_at) VALUES ('trk-1', 'inferred', 't', 'abc', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
        "modified_at, updated_at, origin_device_id) "
        "VALUES ('trk-1', 'bpm', '128', 'rekordbox', ?, NULL, NULL)",
        (_T_LEGACY,),
    )
    conn.execute(
        "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
        "modified_at, updated_at, origin_device_id) "
        "VALUES ('trk-1', 'key', '8A', 'rekordbox', "
        "'2024-04-17T21:53:19.274000+00:00', ?, 'dev-a')",
        (_T_STAMPED,),
    )
    conn.execute(
        "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
        "modified_at, updated_at, origin_device_id) "
        "VALUES ('trk-2', 'bpm', '130', 'rekordbox', ?, NULL, NULL)",
        (_T_LEGACY,),
    )


def test_fresh_ladder_reaches_v15_backfill_marker() -> None:
    """[if] v15 backfill runs once [then] schema_meta_markers records completion, [else stop]."""
    conn = sqlite3.connect(":memory:")
    state_schema.apply_migrations(conn)
    version = conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
    assert version == state_schema.SCHEMA_VERSION
    marker = conn.execute(
        f"SELECT marker FROM {MARKER_TABLE} WHERE marker = ?",
        (TRACK_FIELDS_STAMP_BACKFILL_MARKER,),
    ).fetchone()
    assert marker is not None
    conn.close()


def test_v15_has_no_standalone_sql() -> None:
    """[if] v15 is defined [then] SQL-only backfill is replaced by Python, [else stop]."""
    assert _V15 == []


def test_v15_backfills_only_null_updated_at_from_modified_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] v14 rows have NULL updated_at [then] backfill copies modified_at only, [else stop]."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    state_schema._ensure_meta(conn)
    try:
        for step_idx in range(14):
            for stmt in state_schema.MIGRATIONS[step_idx]:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
                (step_idx + 1, "2026-09-01T00:00:00+00:00"),
            )
        _seed_v14_track_fields(conn)
        conn.commit()
    finally:
        conn.close()

    state_db.open_rw(path).close()
    before_second = sqlite3.connect(str(path))
    track_fields_before = before_second.execute(
        "SELECT COUNT(*) FROM track_fields"
    ).fetchone()[0]
    changelog_before = before_second.execute(
        "SELECT COUNT(*) FROM local_changelog WHERE table_name = 'track_fields'"
    ).fetchone()[0]
    before_second.close()

    state_db.open_rw(path).close()

    migrated = sqlite3.connect(str(path))
    try:
        assert (
            migrated.execute("SELECT COUNT(*) FROM track_fields").fetchone()[0]
            == track_fields_before
        )
        assert (
            migrated.execute(
                "SELECT COUNT(*) FROM local_changelog WHERE table_name = 'track_fields'"
            ).fetchone()[0]
            == changelog_before
        )
        legacy = migrated.execute(
            "SELECT value_json, source, confidence, modified_at, updated_at, origin_device_id "
            "FROM track_fields WHERE field_name = 'bpm'"
        ).fetchone()
        stamped = migrated.execute(
            "SELECT value_json, source, confidence, modified_at, updated_at, origin_device_id "
            "FROM track_fields WHERE field_name = 'key'"
        ).fetchone()
        assert legacy is not None
        assert legacy == ("128", "rekordbox", None, _T_LEGACY, _T_LEGACY, None)
        assert stamped is not None
        assert stamped == ("8A", "rekordbox", None, _T_LEGACY, _T_STAMPED, "dev-a")
        logged = migrated.execute(
            "SELECT row_pk, updated_at FROM local_changelog WHERE table_name = 'track_fields'"
        ).fetchall()
        assert len(logged) == 2
        assert (
            migrated.execute(
                "SELECT COUNT(*) FROM local_changelog WHERE table_name = 'track_fields'"
            ).fetchone()[0]
            == 2
        )
    finally:
        migrated.close()

    fresh = sqlite3.connect(":memory:")
    state_schema.apply_migrations(fresh)
    migrated = sqlite3.connect(str(path))
    assert _columns(migrated, "track_fields") == _columns(fresh, "track_fields")
    migrated.close()
    fresh.close()


def test_v15_backfill_appends_hub_changelog_when_hub_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] MDT_IS_HUB=1 [then] v15 logs hub_changelog not local_changelog, [else stop]."""
    monkeypatch.setenv("MDT_IS_HUB", "1")
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    state_schema._ensure_meta(conn)
    try:
        for step_idx in range(14):
            for stmt in state_schema.MIGRATIONS[step_idx]:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
                (step_idx + 1, "2026-09-01T00:00:00+00:00"),
            )
        _seed_v14_track_fields(conn)
        conn.commit()
    finally:
        conn.close()

    state_db.open_rw(path).close()

    conn = sqlite3.connect(str(path))
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM hub_changelog").fetchone()[0] == 2
        )
        assert conn.execute("SELECT COUNT(*) FROM local_changelog").fetchone()[0] == 0
    finally:
        conn.close()


def test_v15_migration_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] v15 backfill runs twice [then] rows and changelog stay stable, [else stop]."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    state_schema.apply_migrations(conn)
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, content_hash, "
        "created_at, updated_at) VALUES ('trk-1', 'inferred', 't', 'abc', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
        "modified_at, updated_at, origin_device_id) "
        "VALUES ('trk-1', 'bpm', '128', 'rekordbox', ?, NULL, NULL)",
        (_T_LEGACY,),
    )
    conn.execute(
        f"DELETE FROM {MARKER_TABLE} WHERE marker = ?",
        (TRACK_FIELDS_STAMP_BACKFILL_MARKER,),
    )
    conn.commit()
    before = conn.execute(
        "SELECT value_json, modified_at, updated_at FROM track_fields"
    ).fetchone()
    backfill_track_fields_stamps(conn)
    after_once = conn.execute(
        "SELECT value_json, modified_at, updated_at FROM track_fields"
    ).fetchone()
    changelog_once = conn.execute(
        "SELECT COUNT(*) FROM local_changelog WHERE table_name = 'track_fields'"
    ).fetchone()[0]
    backfill_track_fields_stamps(conn)
    after_twice = conn.execute(
        "SELECT value_json, modified_at, updated_at FROM track_fields"
    ).fetchone()
    changelog_twice = conn.execute(
        "SELECT COUNT(*) FROM local_changelog WHERE table_name = 'track_fields'"
    ).fetchone()[0]
    conn.close()
    assert before[0] == after_once[0] == after_twice[0]
    assert after_once == after_twice
    assert after_once[2] == _T_LEGACY
    assert changelog_once == changelog_twice == 1


def test_v15_reoffers_already_stamped_rows_missing_changelog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] #3112 left stamped rows without changelog [then] open re-offers once, [else stop]."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    state_schema.apply_migrations(conn)
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, content_hash, "
        "created_at, updated_at) VALUES ('trk-1', 'inferred', 't', 'abc', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
        "modified_at, updated_at, origin_device_id) "
        "VALUES ('trk-1', 'bpm', '128', 'rekordbox', ?, ?, NULL)",
        (_T_LEGACY, _T_LEGACY),
    )
    conn.execute(
        f"DELETE FROM {MARKER_TABLE} WHERE marker = ?",
        (TRACK_FIELDS_STAMP_BACKFILL_MARKER,),
    )
    conn.commit()
    conn.close()

    state_db.open_rw(path).close()
    state_db.open_rw(path).close()

    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT value_json, source, modified_at, updated_at, origin_device_id "
            "FROM track_fields WHERE field_name = 'bpm'"
        ).fetchone()
        assert row == ("128", "rekordbox", _T_LEGACY, _T_LEGACY, None)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM local_changelog WHERE table_name = 'track_fields'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_v15_reoffers_non_null_origin_rows_missing_changelog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] #3112 stamped a row with domain origin [then] open re-offers without mutating it, [else stop]."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    state_schema.apply_migrations(conn)
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, content_hash, "
        "created_at, updated_at) VALUES ('trk-1', 'inferred', 't', 'abc', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
        "modified_at, updated_at, origin_device_id) "
        "VALUES ('trk-1', 'bpm', '128', 'rekordbox', ?, ?, 'dev-a')",
        (_T_LEGACY, _T_LEGACY),
    )
    conn.execute(
        f"DELETE FROM {MARKER_TABLE} WHERE marker = ?",
        (TRACK_FIELDS_STAMP_BACKFILL_MARKER,),
    )
    conn.commit()
    conn.close()

    state_db.open_rw(path).close()
    state_db.open_rw(path).close()

    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT value_json, source, modified_at, updated_at, origin_device_id "
            "FROM track_fields WHERE field_name = 'bpm'"
        ).fetchone()
        assert row == ("128", "rekordbox", _T_LEGACY, _T_LEGACY, "dev-a")
        logged = conn.execute(
            "SELECT origin_device_id FROM local_changelog WHERE table_name = 'track_fields'"
        ).fetchall()
        assert len(logged) == 1
        assert logged[0][0] == "dev-a"
    finally:
        conn.close()


def test_v15_backfill_rolls_back_row_and_changelog_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] v15 backfill fails mid-pass [then] no row or changelog is claimed, [else stop]."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    state_schema._ensure_meta(conn)
    try:
        for step_idx in range(14):
            for stmt in state_schema.MIGRATIONS[step_idx]:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
                (step_idx + 1, "2026-09-01T00:00:00+00:00"),
            )
        _seed_v14_track_fields(conn)
        conn.commit()
    finally:
        conn.close()

    calls = {"n": 0}
    real_encode = sync_stamp.encode_row_pk

    def _explode_on_second_row(values):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated failure mid-backfill")
        return real_encode(values)

    monkeypatch.setattr(sync_stamp, "encode_row_pk", _explode_on_second_row)
    with pytest.raises(RuntimeError, match="simulated failure"):
        backfill_track_fields_stamps(sqlite3.connect(str(path)))

    failed = sqlite3.connect(str(path))
    try:
        null_count = failed.execute(
            "SELECT COUNT(*) FROM track_fields WHERE updated_at IS NULL"
        ).fetchone()[0]
        changelog_count = failed.execute(
            "SELECT COUNT(*) FROM local_changelog WHERE table_name = 'track_fields'"
        ).fetchone()[0]
        assert null_count == 2
        assert changelog_count == 0
    finally:
        failed.close()

    monkeypatch.setattr(sync_stamp, "encode_row_pk", real_encode)
    state_db.open_rw(path).close()
    recovered = sqlite3.connect(str(path))
    try:
        assert (
            recovered.execute(
                "SELECT COUNT(*) FROM track_fields WHERE updated_at IS NULL"
            ).fetchone()[0]
            == 0
        )
        assert (
            recovered.execute(
                "SELECT COUNT(*) FROM local_changelog WHERE table_name = 'track_fields'"
            ).fetchone()[0]
            == 2
        )
    finally:
        recovered.close()


def test_log_stamp_backfill_rows_uses_supplied_table_for_idempotence(
    tmp_path: Path,
) -> None:
    """[if] relogging primitive gets a table name [then] idempotence keys on it, [else stop]."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    state_schema.apply_migrations(conn)
    pk = ("trk-1", "bpm")
    stamp = _T_LEGACY
    logged_once = log_stamp_backfill_rows(
        conn,
        table="track_fields",
        row_pks=[pk],
        stamp_by_pk={pk: stamp},
        origin_by_pk={pk: None},
    )
    logged_twice = log_stamp_backfill_rows(
        conn,
        table="other_table",
        row_pks=[pk],
        stamp_by_pk={pk: stamp},
        origin_by_pk={pk: None},
    )
    conn.commit()
    counts = conn.execute(
        "SELECT table_name, COUNT(*) FROM local_changelog GROUP BY table_name ORDER BY table_name"
    ).fetchall()
    conn.close()
    assert logged_once == 1
    assert logged_twice == 1
    assert counts == [("other_table", 1), ("track_fields", 1)]


def test_active_changelog_table_follows_mdt_is_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] MDT_IS_HUB toggles [then] active changelog table follows it, [else stop]."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    assert active_changelog_table() == sync_stamp.LOCAL_CHANGELOG_TABLE
    monkeypatch.setenv("MDT_IS_HUB", "1")
    assert active_changelog_table() == HUB_CHANGELOG_TABLE
