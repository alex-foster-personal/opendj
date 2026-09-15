"""Migration v15: legacy track_fields stamp backfill (#3101).

[if] schema v15 is applied [then] NULL updated_at rows copy modified_at, [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import schema as state_schema
from apps.shared.state.migrations_v15 import _V15

pytestmark = [pytest.mark.requirement("INFRA-01"), pytest.mark.requirement("CLOUDSYNC-03")]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def test_fresh_ladder_reaches_v15() -> None:
    """[if] a fresh schema is applied [then] schema_meta records v15, [else stop]."""
    conn = sqlite3.connect(":memory:")
    state_schema.apply_migrations(conn)
    version = conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
    assert version == 15
    conn.close()


def test_v15_backfills_only_null_updated_at_from_modified_at(tmp_path: Path) -> None:
    """[if] v14 rows have NULL updated_at [then] backfill copies modified_at only, [else stop]."""
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
        conn.execute(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, content_hash, "
            "created_at, updated_at) VALUES ('trk-1', 'inferred', 't', 'abc', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
            "modified_at, updated_at, origin_device_id) "
            "VALUES ('trk-1', 'bpm', '128', 'rekordbox', "
            "'2024-04-17T21:53:19.274000+00:00', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
            "modified_at, updated_at, origin_device_id) "
            "VALUES ('trk-1', 'key', '8A', 'rekordbox', "
            "'2024-04-17T21:53:19.274000+00:00', "
            "'2026-08-30T10:00:00.000000+00:00', 'dev-a')"
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(str(path))
    try:
        for stmt in _V15:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (15, ?)",
            ("2026-09-15T00:00:00+00:00",),
        )
        conn.commit()
        legacy = conn.execute(
            "SELECT updated_at FROM track_fields WHERE field_name = 'bpm'"
        ).fetchone()
        stamped = conn.execute(
            "SELECT updated_at FROM track_fields WHERE field_name = 'key'"
        ).fetchone()
        assert legacy is not None and legacy[0] == "2024-04-17T21:53:19.274000+00:00"
        assert stamped is not None and stamped[0] == "2026-08-30T10:00:00.000000+00:00"
    finally:
        conn.close()

    fresh = sqlite3.connect(":memory:")
    state_schema.apply_migrations(fresh)
    migrated = sqlite3.connect(str(path))
    assert _columns(migrated, "track_fields") == _columns(fresh, "track_fields")
    migrated.close()
    fresh.close()


def test_v15_migration_is_idempotent(tmp_path: Path) -> None:
    """[if] v15 backfill runs twice [then] rows are unchanged, [else stop]."""
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
        "VALUES ('trk-1', 'bpm', '128', 'rekordbox', "
        "'2024-04-17T21:53:19.274000+00:00', NULL, NULL)"
    )
    conn.commit()
    before = conn.execute(
        "SELECT value_json, modified_at, updated_at FROM track_fields"
    ).fetchone()
    for stmt in _V15:
        conn.execute(stmt)
    after_once = conn.execute(
        "SELECT value_json, modified_at, updated_at FROM track_fields"
    ).fetchone()
    for stmt in _V15:
        conn.execute(stmt)
    after_twice = conn.execute(
        "SELECT value_json, modified_at, updated_at FROM track_fields"
    ).fetchone()
    conn.close()
    assert before[0] == after_once[0] == after_twice[0]
    assert after_once == after_twice
    assert after_once[2] == "2024-04-17T21:53:19.274000+00:00"
