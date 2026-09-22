"""Local state-authoritative backup and selective restore (issue #2498).

Every refusal here is paired with a subject the same instrument accepts.
Uses real migrated state.db via :func:`apps.shared.state.db.open_rw` (no mocks).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.engine_core.config import EngineConfig
from apps.engine_core.lock import EngineLock
from apps.shared import sqlite_verified_copy as svc
from apps.shared import state_authoritative_backup as sab
from apps.shared.pairings.schema_sql import ensure_phase08_tables
from apps.shared.play_orders.schema import apply_play_order_migrations
from apps.shared.state import db as state_db
from apps.shared.state_authoritative_backup import (
    StateAuthoritativeBackupError,
    backup_state_db,
    list_backups,
    restore_tables,
)

NOW = "2026-09-15T00:00:00+00:00"


def _seed_authoritative_data(data_dir: Path) -> dict[str, object]:
    """Seed ADR-0022 authoritative rows into a fresh migrated state.db."""
    conn = state_db.open_rw(sab.state_db_path(data_dir))
    try:
        conn.execute(
            """
            INSERT INTO tracks(
                stable_id, stable_id_tier, title, created_at, updated_at
            ) VALUES (?, 'inferred', ?, ?, ?)
            """,
            ("track-a", "Probe Track", NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO tracks(
                stable_id, stable_id_tier, title, created_at, updated_at
            ) VALUES (?, 'inferred', ?, ?, ?)
            """,
            ("track-b", "Other Track", NOW, NOW),
        )
        ensure_phase08_tables(conn)
        conn.execute(
            """
            INSERT INTO smartlists(
                id, name, rule, referenced_fields, created_at, modified_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("sl-1", "BPM High", "{}", "[]", NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO pairings(
                from_stable_id, to_stable_id, direction, source,
                created_at, modified_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("track-a", "track-b", "into", "manual", NOW, NOW),
        )
        apply_play_order_migrations(conn)
        conn.execute(
            """
            INSERT INTO playlists(
                playlist_id, name, vendor, vendor_pl_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("pl-1", "Set List", "webui", "webui:pl-1", NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO play_orders(
                playlist_id, name, created_at, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            ("pl-1", "Friday Order", NOW, NOW),
        )
        (order_id,) = conn.execute("SELECT id FROM play_orders").fetchone()
        conn.execute(
            """
            INSERT INTO play_order_entries(
                play_order_id, stable_id, position
            ) VALUES (?, ?, ?)
            """,
            (order_id, "track-a", 1),
        )
        conn.executemany(
            """
            INSERT INTO track_fields(
                stable_id, field_name, value_json, source, modified_at
            ) VALUES (?, ?, ?, 'webui', ?)
            """,
            [
                ("track-a", "notes", '"note text"', NOW),
                ("track-a", "tags", '["tag1"]', NOW),
                ("track-a", "rating", '"4"', NOW),
            ],
        )
        conn.commit()
        return {
            "smartlist_id": "sl-1",
            "order_id": order_id,
            "track_a_title": "Probe Track",
        }
    finally:
        conn.close()


def _corrupt_smartlists_index(db: Path) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        (root,) = conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE name = 'idx_smartlists_name'"
        ).fetchone()
    finally:
        conn.close()
    raw = bytearray(db.read_bytes())
    start = (root - 1) * page_size
    page = bytes(raw[start : start + page_size])
    needle = b"BPM High"
    offset = page.find(needle)
    assert offset >= 0
    raw[start + offset : start + offset + len(needle)] = b"CORRUPT!"
    db.write_bytes(bytes(raw))


@pytest.fixture
def machine_data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "machine"
    _seed_authoritative_data(data_dir)
    return data_dir


@pytest.mark.requirement("LIBM-112")
def test_backup_creates_verified_dated_copy_without_cloudsync(
    machine_data_dir: Path, tmp_path: Path
) -> None:
    """[if] an unenrolled machine has authoritative rows [then] backup creates a verified dated copy, [else stop]."""
    backup = backup_state_db(machine_data_dir, tmp_path / "backups", keep=3)
    assert backup.path.name.startswith("state-authoritative-")
    assert backup.path.name.endswith("UTC.db")
    assert backup.row_counts["smartlists"] == 1
    assert backup.row_counts["pairings"] == 1


@pytest.mark.requirement("LIBM-113")
def test_selective_restore_smartlists_leaves_tracks_untouched(
    machine_data_dir: Path, tmp_path: Path
) -> None:
    """[if] only smartlists are restored from backup [then] tracks remain unchanged, [else stop]."""
    dest = tmp_path / "backups"
    backup = backup_state_db(machine_data_dir, dest, keep=3)
    live = sab.state_db_path(machine_data_dir)
    conn = sqlite3.connect(live)
    try:
        before_tracks = conn.execute("SELECT stable_id, title FROM tracks ORDER BY stable_id").fetchall()
        before_count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    finally:
        conn.close()

    _corrupt_smartlists_index(live)
    with pytest.raises(svc.VerifiedCopyError, match="integrity_check|malformed"):
        svc.verify_backup(live)

    restored = restore_tables(backup.path, machine_data_dir, ["smartlists"])
    assert restored == ["smartlists"]

    conn = sqlite3.connect(live)
    try:
        assert conn.execute("SELECT COUNT(*) FROM smartlists").fetchone()[0] == 1
        assert conn.execute("SELECT name FROM smartlists").fetchone()[0] == "BPM High"
        after_tracks = conn.execute("SELECT stable_id, title FROM tracks ORDER BY stable_id").fetchall()
        after_count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        verdict = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        assert verdict == ["ok"]
    finally:
        conn.close()
    assert after_count == before_count
    assert after_tracks == before_tracks


@pytest.mark.requirement("LIBM-112")
def test_corrupted_source_is_refused_and_leaves_no_backup(
    machine_data_dir: Path, tmp_path: Path
) -> None:
    """[if] the live state.db fails integrity check [then] backup refuses and writes no file, [else stop]."""
    dest = tmp_path / "backups"
    live = sab.state_db_path(machine_data_dir)
    _corrupt_smartlists_index(live)
    with pytest.raises(StateAuthoritativeBackupError, match="integrity_check|malformed|could not copy"):
        backup_state_db(machine_data_dir, dest, keep=3)
    assert list(dest.iterdir()) == []


def _drop_a_row_from_partials(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    real_verify = svc.verify_backup
    damaged: list[Path] = []

    def verify_after_losing_a_row(path: Path, *, empty_message: str | None = None) -> dict[str, int]:
        if Path(path).name.startswith(svc.VerifiedCopyCFG.PARTIAL_PREFIX):
            conn = sqlite3.connect(path)
            try:
                conn.execute("DELETE FROM smartlists")
                conn.commit()
            finally:
                conn.close()
            damaged.append(Path(path))
        return real_verify(path, empty_message=empty_message)

    monkeypatch.setattr(svc, "verify_backup", verify_after_losing_a_row)
    return damaged


@pytest.mark.requirement("LIBM-112")
def test_backup_row_count_mismatch_deletes_partial(
    machine_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] verified copy row counts diverge mid-backup [then] partial is deleted and backup raises, [else stop]."""
    dest = tmp_path / "backups"
    damaged = _drop_a_row_from_partials(monkeypatch)
    with pytest.raises(StateAuthoritativeBackupError, match="row counts differ"):
        backup_state_db(machine_data_dir, dest, keep=3)
    assert len(damaged) == 1
    assert list(dest.iterdir()) == []


def test_restore_refuses_while_engine_lock_held(
    machine_data_dir: Path, tmp_path: Path
) -> None:
    """Safety: no writes under live engine lock."""
    backup = backup_state_db(machine_data_dir, tmp_path / "backups", keep=3)
    live = sab.state_db_path(machine_data_dir)
    before = live.read_bytes()
    with (
        EngineLock(EngineConfig(data_dir=machine_data_dir).lock_path, role="pytest-engine"),
        pytest.raises(StateAuthoritativeBackupError, match="live engine"),
    ):
        restore_tables(backup.path, machine_data_dir, ["smartlists"])
    assert live.read_bytes() == before


@pytest.mark.requirement("LIBM-113")
def test_restore_play_orders_includes_entries(
    machine_data_dir: Path, tmp_path: Path
) -> None:
    """[if] play_orders is restored from backup [then] entries are restored together, [else stop]."""
    backup = backup_state_db(machine_data_dir, tmp_path / "backups", keep=3)
    live = sab.state_db_path(machine_data_dir)
    conn = sqlite3.connect(live)
    try:
        conn.execute("DELETE FROM play_order_entries")
        conn.execute("DELETE FROM play_orders")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM play_orders").fetchone()[0] == 0
    finally:
        conn.close()

    restore_tables(backup.path, machine_data_dir, ["play_orders"])

    conn = sqlite3.connect(live)
    try:
        assert conn.execute("SELECT COUNT(*) FROM play_orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM play_order_entries").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.requirement("LIBM-113")
def test_restore_track_fields_notes_tags_only(
    machine_data_dir: Path, tmp_path: Path
) -> None:
    """[if] track_fields notes and tags are restored [then] rating rows remain untouched, [else stop]."""
    backup = backup_state_db(machine_data_dir, tmp_path / "backups", keep=3)
    live = sab.state_db_path(machine_data_dir)
    conn = sqlite3.connect(live)
    try:
        conn.execute("DELETE FROM track_fields WHERE field_name IN ('notes', 'tags')")
        conn.execute(
            "UPDATE track_fields SET value_json = '\"1\"' WHERE field_name = 'rating'"
        )
        conn.commit()
    finally:
        conn.close()

    restore_tables(backup.path, machine_data_dir, ["track_fields_notes_tags"])

    conn = sqlite3.connect(live)
    try:
        notes = conn.execute(
            "SELECT value_json FROM track_fields WHERE field_name = 'notes'"
        ).fetchone()[0]
        rating = conn.execute(
            "SELECT value_json FROM track_fields WHERE field_name = 'rating'"
        ).fetchone()[0]
        assert notes == '"note text"'
        assert rating == '"1"'
    finally:
        conn.close()


def test_cli_backup_and_selective_restore_exit_codes(
    machine_data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI exits 0 on success and 1 on refused restore."""
    dest = tmp_path / "backups"
    assert (
        sab.main(
            ["backup", "--data-dir", str(machine_data_dir), "--dest", str(dest), "--keep", "2"]
        )
        == 0
    )
    assert "[OK] backup" in capsys.readouterr().out
    (newest,) = list_backups(dest)
    live = sab.state_db_path(machine_data_dir)
    conn = sqlite3.connect(live)
    try:
        conn.execute("DELETE FROM smartlists")
        conn.commit()
    finally:
        conn.close()
    assert (
        sab.main(
            [
                "restore",
                "--backup",
                str(newest),
                "--data-dir",
                str(machine_data_dir),
                "--table",
                "smartlists",
            ]
        )
        == 0
    )
    assert "[OK] restored smartlists" in capsys.readouterr().out
    with EngineLock(EngineConfig(data_dir=machine_data_dir).lock_path, role="pytest-engine"):
        assert (
            sab.main(
                [
                    "restore",
                    "--backup",
                    str(newest),
                    "--data-dir",
                    str(machine_data_dir),
                    "--table",
                    "smartlists",
                ]
            )
            == 1
        )
    assert "live engine" in capsys.readouterr().err


def test_keep_retains_only_the_newest_backups(machine_data_dir: Path, tmp_path: Path) -> None:
    dest = tmp_path / "backups"
    base = datetime(2026, 9, 11, 3, 30, tzinfo=UTC)
    for day in range(4):
        backup_state_db(machine_data_dir, dest, keep=2, now=base + timedelta(days=day))
    assert [p.name for p in list_backups(dest)] == [
        "state-authoritative-20260914T033000UTC.db",
        "state-authoritative-20260913T033000UTC.db",
    ]
