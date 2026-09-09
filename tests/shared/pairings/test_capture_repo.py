"""Durable pairing-capture schema and repository tests.

[if] capture tables differ [then] migration adopts only safe shapes, [else stop].
"""
from __future__ import annotations

import sqlite3

import pytest

from apps.shared.pairings.capture_repo import PairingCaptureError, PairingCaptureRepo
from apps.shared.pairings.schema_sql import apply_pairing_capture_migrations

pytestmark = pytest.mark.requirement("PAIR-01")


def test_capture_migration_creates_tables_and_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        assert apply_pairing_capture_migrations(conn) == 1
        assert apply_pairing_capture_migrations(conn) == 1
        assert PairingCaptureRepo(conn).list_snapshots() == []
    finally:
        conn.close()


def test_capture_migration_adopts_existing_live_tables_without_data_loss() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute(
            "CREATE TABLE pairing_sync_snapshots ("
            "id TEXT PRIMARY KEY, stable_a TEXT NOT NULL, stable_b TEXT NOT NULL, "
            "master_side TEXT NOT NULL, sync_mode TEXT NOT NULL, "
            "a_tempo_ratio REAL NOT NULL, b_tempo_ratio REAL NOT NULL, "
            "a_position_beat_n INTEGER, a_position_phase REAL, "
            "a_position_ms REAL NOT NULL, b_position_beat_n INTEGER, "
            "b_position_phase REAL, b_position_ms REAL NOT NULL, "
            "captured_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO pairing_sync_snapshots VALUES "
            "('existing', 'a', 'b', 'a', 'beat', 1, 1, NULL, NULL, 0, NULL, NULL, 0, 'now')"
        )
        conn.execute(
            "CREATE TABLE pairing_alignments ("
            "id TEXT PRIMARY KEY, stable_a TEXT NOT NULL, stable_b TEXT NOT NULL, "
            "anchor_a_kind TEXT NOT NULL, anchor_b_kind TEXT NOT NULL, "
            "anchor_a_slot TEXT, anchor_b_slot TEXT, anchor_a_ms REAL NOT NULL, "
            "anchor_b_ms REAL NOT NULL, label TEXT, created_at TEXT NOT NULL)"
        )

        assert apply_pairing_capture_migrations(conn) == 1
        assert conn.execute(
            "SELECT id FROM pairing_sync_snapshots"
        ).fetchone() == ("existing",)
        assert conn.execute(
            "SELECT version FROM pairing_capture_schema_meta"
        ).fetchone() == (1,)
    finally:
        conn.close()


def test_capture_migration_rejects_incompatible_existing_table() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute("CREATE TABLE pairing_sync_snapshots (id TEXT PRIMARY KEY)")
        with pytest.raises(RuntimeError, match="missing required columns"):
            apply_pairing_capture_migrations(conn)
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='pairing_capture_schema_meta'"
        ).fetchone() is None
    finally:
        conn.close()


def test_capture_repo_round_trips_snapshot_and_alignment() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        repo = PairingCaptureRepo(conn)
        snapshot = repo.add_snapshot(
            stable_a="a",
            stable_b="b",
            master_side="a",
            sync_mode="beat",
            a_tempo_ratio=1.0,
            b_tempo_ratio=0.99,
            a_position_ms=123.0,
            b_position_ms=456.0,
            a_position_beat_n=17,
        )
        alignment = repo.add_alignment(
            stable_a="a",
            stable_b="b",
            anchor_a_kind="hotcue",
            anchor_b_kind="ms",
            anchor_a_ms=123.0,
            anchor_b_ms=456.0,
            anchor_a_slot="A",
        )
        assert repo.get_snapshot(snapshot.id) == snapshot
        assert repo.list_snapshots(stable_a="a") == [snapshot]
        assert repo.list_alignments(stable_a="b") == [alignment]
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"stable_a": "same", "stable_b": "same"}, "distinct tracks"),
        ({"master_side": "middle"}, "master_side"),
        ({"sync_mode": "free"}, "sync_mode"),
        ({"a_position_ms": -1.0}, "non-negative"),
    ],
)
def test_snapshot_rejects_invalid_values(
    kwargs: dict[str, object], message: str
) -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        repo = PairingCaptureRepo(conn)
        values: dict[str, object] = {
            "stable_a": "a",
            "stable_b": "b",
            "master_side": "a",
            "sync_mode": "beat",
            "a_tempo_ratio": 1.0,
            "b_tempo_ratio": 1.0,
            "a_position_ms": 0.0,
            "b_position_ms": 0.0,
        }
        values.update(kwargs)
        with pytest.raises(PairingCaptureError, match=message):
            repo.add_snapshot(**values)  # type: ignore[arg-type]
    finally:
        conn.close()
