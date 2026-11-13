"""Local state-authoritative backup and selective restore (issue #2498).

Every refusal here is paired with a subject the same instrument accepts.
Uses real migrated state.db via :func:`apps.shared.state.db.open_rw` (no mocks).
"""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.engine_core.config import EngineConfig
from apps.engine_core.lock import EngineLock
from apps.shared import sqlite_verified_copy as svc
from apps.shared import state_authoritative_backup as sab
from apps.shared import state_authoritative_backup_schedule as sched
from apps.shared.pairings.schema_sql import ensure_phase08_tables
from apps.shared.play_orders.schema import apply_play_order_migrations
from apps.shared.state import db as state_db
from apps.shared.state_authoritative_backup import (
    PREFLIGHT_PARTIAL_PROBE_SUFFIX,
    StateAuthoritativeBackupError,
    backup_state_db,
    describe_backups,
    list_backups,
    plan_restore,
    preview_track_fields_notes_tags_restore,
    restore_tables,
)

NOW = "2026-09-15T00:00:00+00:00"
HISTORY_AT = "2026-09-14T12:00:00+00:00"


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
        (root,) = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'idx_smartlists_name'").fetchone()
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
def test_backup_creates_verified_dated_copy_without_cloudsync(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] an unenrolled machine has authoritative rows [then] backup creates a verified dated copy, [else stop]."""
    backup = backup_state_db(machine_data_dir, tmp_path / "backups", keep=3)
    assert backup.path.name.startswith("state-authoritative-")
    assert backup.path.name.endswith("UTC.db")
    assert backup.row_counts["smartlists"] == 1
    assert backup.row_counts["pairings"] == 1


@pytest.mark.requirement("LIBM-112")
@pytest.mark.requirement("LIBM-14784")
def test_backup_default_dest_creates_missing_backup_tree(machine_data_dir: Path) -> None:
    """[if] default backup dir is missing [then] backup creates tree and succeeds, [else stop]."""
    backup_dir = sab.default_backup_dir(machine_data_dir)
    assert not backup_dir.exists()
    backup = backup_state_db(machine_data_dir)
    assert backup_dir.is_dir()
    assert backup.path.parent == backup_dir
    assert backup.path.name.startswith("state-authoritative-")
    assert backup.path.name.endswith("UTC.db")
    assert backup.row_counts["smartlists"] == 1
    assert backup.row_counts["pairings"] == 1


@pytest.mark.requirement("LIBM-113")
def test_selective_restore_smartlists_leaves_tracks_untouched(machine_data_dir: Path, tmp_path: Path) -> None:
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
def test_corrupted_source_is_refused_and_leaves_no_backup(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] the live state.db fails integrity check [then] backup refuses and writes no file, [else stop]."""
    dest = tmp_path / "backups"
    live = sab.state_db_path(machine_data_dir)
    _corrupt_smartlists_index(live)
    with pytest.raises(StateAuthoritativeBackupError, match="integrity_check|malformed|could not copy"):
        backup_state_db(machine_data_dir, dest, keep=3)
    assert list(dest.iterdir()) == []


def _assert_dest_clean(dest: Path) -> None:
    if not dest.exists():
        return
    assert not any(p.name.startswith("state-authoritative-") for p in dest.iterdir())
    assert list(dest.iterdir()) == []


def _copy_snapshot_spy(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    called: list[bool] = [False]
    real = svc.copy_snapshot

    def wrapped(*args: object, **kwargs: object) -> None:
        called[0] = True
        real(*args, **kwargs)

    monkeypatch.setattr(svc, "copy_snapshot", wrapped)
    return called


@pytest.mark.requirement("LIBM-112")
@pytest.mark.requirement("LIBM-14784")
def test_backup_refuses_non_writable_destination(
    machine_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] destination directory is not writable [then] backup refuses before copy, [else stop]."""
    dest = tmp_path / "backups"
    dest.mkdir()
    snapshot_calls = _copy_snapshot_spy(monkeypatch)
    original_mode = dest.stat().st_mode
    dest.chmod(0o555)
    try:
        if os.access(dest, os.W_OK):
            pytest.fail("chmod did not make dest unwritable (running as root?)")
        with pytest.raises(StateAuthoritativeBackupError, match="not writable"):
            backup_state_db(machine_data_dir, dest, keep=3)
    finally:
        dest.chmod(original_mode)
    assert snapshot_calls[0] is False
    _assert_dest_clean(dest)


@pytest.mark.requirement("LIBM-112")
@pytest.mark.requirement("LIBM-14784")
def test_backup_refuses_when_destination_is_file(
    machine_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] destination path is a file [then] backup refuses before copy, [else stop]."""
    dest = tmp_path / "backups"
    dest.write_text("not a dir")
    snapshot_calls = _copy_snapshot_spy(monkeypatch)
    with pytest.raises(StateAuthoritativeBackupError, match="file, not a directory"):
        backup_state_db(machine_data_dir, dest, keep=3)
    assert snapshot_calls[0] is False
    assert dest.read_text() == "not a dir"


@pytest.mark.requirement("LIBM-112")
@pytest.mark.requirement("LIBM-14784")
def test_backup_refuses_when_partial_probe_cannot_be_created(
    machine_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] destination cannot create a partial file [then] backup refuses before copy, [else stop]."""
    dest = tmp_path / "backups"
    dest.mkdir()
    blocker = dest / f"{svc.VerifiedCopyCFG.PARTIAL_PREFIX}{PREFLIGHT_PARTIAL_PROBE_SUFFIX}"
    blocker.mkdir()
    snapshot_calls = _copy_snapshot_spy(monkeypatch)
    with pytest.raises(StateAuthoritativeBackupError, match="cannot create a partial backup file"):
        backup_state_db(machine_data_dir, dest, keep=3)
    assert snapshot_calls[0] is False
    assert not any(p.name.startswith("state-authoritative-") for p in dest.iterdir())


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


def test_restore_refuses_while_engine_lock_held(machine_data_dir: Path, tmp_path: Path) -> None:
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
def test_restore_play_orders_includes_entries(machine_data_dir: Path, tmp_path: Path) -> None:
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


@pytest.mark.requirement("PAIR-04")
def test_restore_pairings_from_backup_taken_before_snapshot_json(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] a backup's pairings table predates snapshot_json [then] it still restores, [else stop]."""
    backup = backup_state_db(machine_data_dir, tmp_path / "backups", keep=3)
    old = sqlite3.connect(backup.path)
    try:
        old.execute("ALTER TABLE pairings DROP COLUMN snapshot_json")
        old.commit()
        cols = [row[1] for row in old.execute("PRAGMA table_info(pairings)")]
    finally:
        old.close()
    assert "snapshot_json" not in cols
    live = sab.state_db_path(machine_data_dir)
    conn = sqlite3.connect(live)
    try:
        conn.execute("DELETE FROM pairings")
        conn.commit()
    finally:
        conn.close()

    assert restore_tables(backup.path, machine_data_dir, ["pairings"]) == ["pairings"]

    conn = sqlite3.connect(live)
    try:
        rows = conn.execute("SELECT from_stable_id, to_stable_id, direction, snapshot_json FROM pairings").fetchall()
    finally:
        conn.close()
    assert rows == [("track-a", "track-b", "into", None)]


@pytest.mark.requirement("LIBM-113")
@pytest.mark.requirement("LIBEDIT-14765")
def test_restore_track_fields_notes_tags_only(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] notes/tags restore runs [then] matching history restores and rating history stays, [else stop]."""
    live = sab.state_db_path(machine_data_dir)
    conn = sqlite3.connect(live)
    try:
        conn.executemany(
            """
            INSERT INTO track_field_history(
                stable_id, field_name, value_json, source,
                confidence, modified_at, superseded_at
            ) VALUES (?, ?, ?, 'webui', NULL, ?, ?)
            """,
            [
                ("track-a", "notes", '"old note"', NOW, HISTORY_AT),
                ("track-a", "tags", '["old-tag"]', NOW, HISTORY_AT),
                ("track-a", "rating", '"3"', NOW, HISTORY_AT),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    backup = backup_state_db(machine_data_dir, tmp_path / "backups", keep=3)
    conn = sqlite3.connect(live)
    try:
        conn.execute("DELETE FROM track_fields WHERE field_name IN ('notes', 'tags')")
        conn.execute("DELETE FROM track_field_history WHERE field_name IN ('notes', 'tags')")
        conn.execute("UPDATE track_fields SET value_json = '\"1\"' WHERE field_name = 'rating'")
        conn.execute(
            """
            INSERT INTO track_field_history(
                stable_id, field_name, value_json, source,
                confidence, modified_at, superseded_at
            ) VALUES (?, 'rating', ?, 'webui', NULL, ?, ?)
            """,
            ("track-a", '"2"', NOW, "2026-09-16T00:00:00+00:00"),
        )
        conn.commit()
        rating_history_before_restore = conn.execute(
            """
            SELECT stable_id, field_name, value_json, superseded_at
            FROM track_field_history
            WHERE field_name = 'rating'
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()

    restore_tables(backup.path, machine_data_dir, ["track_fields_notes_tags"])

    conn = sqlite3.connect(live)
    try:
        notes = conn.execute("SELECT value_json FROM track_fields WHERE field_name = 'notes'").fetchone()[0]
        rating = conn.execute("SELECT value_json FROM track_fields WHERE field_name = 'rating'").fetchone()[0]
        assert notes == '"note text"'
        assert rating == '"1"'

        notes_history = conn.execute(
            """
            SELECT value_json, superseded_at FROM track_field_history
            WHERE field_name = 'notes'
            """
        ).fetchall()
        tags_history = conn.execute(
            """
            SELECT value_json, superseded_at FROM track_field_history
            WHERE field_name = 'tags'
            """
        ).fetchall()
        assert notes_history == [('"old note"', HISTORY_AT)]
        assert tags_history == [('["old-tag"]', HISTORY_AT)]

        rating_history_after = conn.execute(
            """
            SELECT stable_id, field_name, value_json, superseded_at
            FROM track_field_history
            WHERE field_name = 'rating'
            ORDER BY id
            """
        ).fetchall()
        assert rating_history_after == rating_history_before_restore
    finally:
        conn.close()


def test_cli_backup_and_selective_restore_exit_codes(
    machine_data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI exits 0 on success and 1 on refused restore."""
    dest = tmp_path / "backups"
    assert sab.main(["backup", "--data-dir", str(machine_data_dir), "--dest", str(dest), "--keep", "2"]) == 0
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


def _live_db_bytes_and_mtime(data_dir: Path) -> tuple[bytes, int]:
    """Return the live state.db bytes and mtime so a test can prove nothing was written."""
    live = sab.state_db_path(data_dir)
    return live.read_bytes(), live.stat().st_mtime_ns


@pytest.mark.requirement("LIBM-13423")
def test_restore_dry_run_reports_scope_without_writing(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] a valid backup and table selection are supplied with dry-run [then] each group reports backup and live row counts, [else stop]."""
    backup = backup_state_db(machine_data_dir, tmp_path / "backups", keep=3)
    conn = sqlite3.connect(sab.state_db_path(machine_data_dir))
    try:
        conn.execute("DELETE FROM smartlists")
        conn.commit()
    finally:
        conn.close()
    before = _live_db_bytes_and_mtime(machine_data_dir)

    plan = plan_restore(backup.path, machine_data_dir, ["smartlists", "track_fields_notes_tags", "play_orders"])

    by_table = {entry.table: entry for entry in plan}
    assert [entry.table for entry in plan] == ["smartlists", "track_fields_notes_tags", "play_orders"]
    assert (by_table["smartlists"].backup_rows, by_table["smartlists"].live_rows) == (1, 0)
    assert (by_table["track_fields_notes_tags"].backup_rows, by_table["track_fields_notes_tags"].live_rows) == (2, 2)
    assert by_table["play_orders"].physical_tables == ("play_order_entries", "play_orders", "play_orders_schema_meta")
    assert _live_db_bytes_and_mtime(machine_data_dir) == before


@pytest.mark.requirement("LIBM-13423")
def test_restore_dry_run_refuses_unknown_table(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] an unknown table is supplied with dry-run [then] it is refused naming the allowed groups, [else stop]."""
    backup = backup_state_db(machine_data_dir, tmp_path / "backups", keep=3)
    with pytest.raises(StateAuthoritativeBackupError, match=r"unknown --table name\(s\) \['tracks'\]; allowed: "):
        plan_restore(backup.path, machine_data_dir, ["smartlists", "tracks"])
    assert [entry.table for entry in plan_restore(backup.path, machine_data_dir, ["smartlists"])] == ["smartlists"]


@pytest.mark.requirement("LIBM-13423")
def test_cli_restore_dry_run_runs_under_engine_lock_and_writes_nothing(
    machine_data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] dry-run completes successfully [then] it exits 0 with the plan and leaves the live rows as they were, [else stop]."""
    dest = tmp_path / "backups"
    backup = backup_state_db(machine_data_dir, dest, keep=3)
    conn = sqlite3.connect(sab.state_db_path(machine_data_dir))
    try:
        conn.execute("DELETE FROM smartlists")
        conn.commit()
    finally:
        conn.close()
    before = _live_db_bytes_and_mtime(machine_data_dir)
    argv = ["restore", "--backup", str(backup.path), "--data-dir", str(machine_data_dir), "--table", "smartlists"]
    with EngineLock(EngineConfig(data_dir=machine_data_dir).lock_path, role="pytest-engine"):
        assert sab.main([*argv, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "[DRY-RUN] smartlists (smartlists): 0 live rows would be replaced by 1" in out
    assert '"dry_run": true' in out
    assert _live_db_bytes_and_mtime(machine_data_dir) == before
    conn = sqlite3.connect(sab.state_db_path(machine_data_dir))
    try:
        assert conn.execute("SELECT COUNT(*) FROM smartlists").fetchone()[0] == 0
    finally:
        conn.close()


def _all_track_fields(data_dir: Path) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(sab.state_db_path(data_dir))
    try:
        return conn.execute(
            "SELECT stable_id, field_name, value_json FROM track_fields ORDER BY stable_id, field_name"
        ).fetchall()
    finally:
        conn.close()


@pytest.mark.requirement("LIBEDIT-14785")
def test_preview_notes_tags_reports_conflicts_when_live_differs(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] live notes differ from backup [then] preview lists conflicts, [else stop]."""
    backup = backup_state_db(machine_data_dir, tmp_path / "backups", keep=3)
    live = sab.state_db_path(machine_data_dir)
    conn = sqlite3.connect(live)
    try:
        conn.execute("UPDATE track_fields SET value_json = '\"changed\"' WHERE field_name = 'notes'")
        conn.commit()
    finally:
        conn.close()

    preview = preview_track_fields_notes_tags_restore(backup.path, machine_data_dir)
    assert preview.matches_backup is False
    assert preview.live_counts["notes"] == 1
    assert preview.backup_counts["notes"] == 1
    assert len(preview.value_conflicts) == 1
    assert preview.value_conflicts[0]["field_name"] == "notes"
    assert preview.value_conflicts[0]["live_value_json"] == '"changed"'
    assert preview.value_conflicts[0]["backup_value_json"] == '"note text"'


@pytest.mark.requirement("LIBEDIT-14785")
def test_preview_notes_tags_reports_match_when_unchanged(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] live notes/tags match backup [then] preview reports match, [else stop]."""
    backup = backup_state_db(machine_data_dir, tmp_path / "backups", keep=3)
    preview = preview_track_fields_notes_tags_restore(backup.path, machine_data_dir)
    assert preview.matches_backup is True
    assert preview.live_counts == preview.backup_counts
    assert preview.value_conflicts == ()
    assert preview.live_only == ()
    assert preview.backup_only == ()


@pytest.mark.requirement("LIBEDIT-14785")
def test_preview_notes_tags_does_not_mutate_live_db(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] preview runs on dirty live notes [then] live rows stay dirty, [else stop]."""
    backup = backup_state_db(machine_data_dir, tmp_path / "backups", keep=3)
    live = sab.state_db_path(machine_data_dir)
    conn = sqlite3.connect(live)
    try:
        conn.execute("UPDATE track_fields SET value_json = '\"dirty\"' WHERE field_name = 'notes'")
        conn.commit()
    finally:
        conn.close()
    before = _all_track_fields(machine_data_dir)

    preview_track_fields_notes_tags_restore(backup.path, machine_data_dir)

    after = _all_track_fields(machine_data_dir)
    assert after == before
    assert any(row[2] == '"dirty"' for row in after if row[1] == "notes")


def test_validate_restore_tables_rejects_unknown_name() -> None:
    """[if] unknown restore table [then] validation raises with allowed list, [else stop]."""
    with pytest.raises(StateAuthoritativeBackupError, match="unknown --table name"):
        sab._validate_restore_tables(["not_a_real_group"])
    with pytest.raises(StateAuthoritativeBackupError, match="allowed:"):
        sab._validate_restore_tables(["not_a_real_group"])


@pytest.mark.requirement("LIBEDIT-14785")
def test_cli_restore_preview_emits_json_and_ok_line(
    machine_data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] CLI restore --preview [then] exit 0 and JSON counts appear, [else stop]."""
    backup = backup_state_db(machine_data_dir, tmp_path / "backups", keep=3)
    assert (
        sab.main(
            [
                "restore",
                "--backup",
                str(backup.path),
                "--data-dir",
                str(machine_data_dir),
                "--table",
                "track_fields_notes_tags",
                "--preview",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert '"live_counts"' in out
    assert '"matches_backup": true' in out
    assert "[OK] preview track_fields_notes_tags" in out


@pytest.mark.requirement("LIBEDIT-14785")
def test_restore_preview_refuses_non_notes_tags_table(
    machine_data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] preview with smartlists table [then] CLI exits 1, [else stop]."""
    backup = backup_state_db(machine_data_dir, tmp_path / "backups", keep=3)
    assert (
        sab.main(
            [
                "restore",
                "--backup",
                str(backup.path),
                "--data-dir",
                str(machine_data_dir),
                "--table",
                "smartlists",
                "--preview",
            ]
        )
        == 1
    )
    assert "preview is only supported" in capsys.readouterr().err


def test_keep_retains_only_the_newest_backups(machine_data_dir: Path, tmp_path: Path) -> None:
    dest = tmp_path / "backups"
    base = datetime(2026, 9, 11, 3, 30, tzinfo=UTC)
    for day in range(4):
        backup_state_db(machine_data_dir, dest, keep=2, now=base + timedelta(days=day))
    assert [p.name for p in list_backups(dest)] == [
        "state-authoritative-20260914T033000UTC.db",
        "state-authoritative-20260913T033000UTC.db",
    ]


def _audit_lines(data_dir: Path) -> list[dict[str, object]]:
    """Parse every record in the data-dir-local restore audit file, or none when absent."""
    path = sab.restore_audit_path(data_dir)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.requirement("LIBM-13433")
def test_committed_restore_appends_audit_record(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] selective restore commits successfully [then] one audit record names backup, tables and time, [else stop]."""
    backup = backup_state_db(machine_data_dir, tmp_path / "backups", keep=3)
    when = datetime(2026, 11, 13, 9, 30, tzinfo=UTC)
    restore_tables(backup.path, machine_data_dir, ["smartlists"], now=when)
    assert sab.restore_audit_path(machine_data_dir).parent == sab.default_backup_dir(machine_data_dir)
    assert _audit_lines(machine_data_dir) == [
        {
            "event": "state_authoritative_restore",
            "restored_at": when.isoformat(),
            "backup": str(backup.path.resolve()),
            "tables": ["smartlists"],
        }
    ]


@pytest.mark.requirement("LIBM-13433")
def test_restore_failing_verification_writes_no_audit_record(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] selective restore fails verification [then] no audit record is written, [else stop]."""
    backup = backup_state_db(machine_data_dir, tmp_path / "backups", keep=3)
    _corrupt_smartlists_index(backup.path)
    with pytest.raises(StateAuthoritativeBackupError):
        restore_tables(backup.path, machine_data_dir, ["smartlists"])
    assert _audit_lines(machine_data_dir) == []


@pytest.mark.requirement("LIBM-13433")
def test_multi_group_restore_appends_one_record_per_restore(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] multiple table groups are restored together [then] one record lists every group, [else stop]."""
    backup = backup_state_db(machine_data_dir, tmp_path / "backups", keep=3)
    restore_tables(backup.path, machine_data_dir, ["smartlists"])
    restore_tables(backup.path, machine_data_dir, ["smartlists", "pairings", "play_orders"])
    records = _audit_lines(machine_data_dir)
    assert [r["tables"] for r in records] == [["smartlists"], ["smartlists", "pairings", "play_orders"]]
    assert all(r["backup"] == str(backup.path.resolve()) for r in records)


@pytest.mark.requirement("LIBM-14772")
def test_describe_backups_age_and_retention_for_multiple_dated_backups(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] dest holds multiple dated backups [then] ages and keep flags match ranks, [else stop]."""
    dest = tmp_path / "backups"
    base = datetime(2026, 9, 11, 3, 30, tzinfo=UTC)
    for day in range(4):
        backup_state_db(machine_data_dir, dest, keep=99, now=base + timedelta(days=day))
    reference = base + timedelta(days=4)
    entries = describe_backups(dest, keep=2, now=reference)
    assert [e.path.name for e in entries] == [p.name for p in list_backups(dest)]
    assert len(entries) == 4
    for rank, entry in enumerate(entries):
        assert entry.retention_rank == rank
        assert entry.retained_under_keep is (rank < 2)
        expected_age = int((reference - (base + timedelta(days=(3 - rank)))).total_seconds())
        assert entry.age_seconds == max(0, expected_age)


@pytest.mark.requirement("LIBM-14772")
def test_describe_backups_refuses_invalid_backup_stamp(tmp_path: Path) -> None:
    """[if] a backup filename has an invalid timestamp [then] describe_backups refuses, [else stop]."""
    dest = tmp_path / "backups"
    dest.mkdir(parents=True)
    (dest / "state-authoritative-not-a-stampUTC.db").write_bytes(b"")
    with pytest.raises(StateAuthoritativeBackupError, match="invalid backup timestamp"):
        describe_backups(dest)


@pytest.mark.requirement("LIBM-14772")
def test_cli_list_json_emits_structured_backups(
    machine_data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] list is requested with --json [then] stdout carries age and retention fields, [else stop]."""
    dest = tmp_path / "backups"
    backup_state_db(machine_data_dir, dest, keep=3)
    assert sab.main(["list", "--dest", str(dest), "--keep", "14", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.splitlines()[0])
    assert payload["keep"] == 14
    assert len(payload["backups"]) >= 1
    for row in payload["backups"]:
        assert "age_seconds" in row
        assert "retained_under_keep" in row
        assert "taken_at" in row


def _systemd_key(text: str, key: str) -> list[str]:
    return [line.split("=", 1)[1] for line in text.splitlines() if line.startswith(f"{key}=")]


def _systemd_argv(exec_start: str) -> list[str]:
    return [word.replace("%%", "%").replace("$$", "$") for word in shlex.split(exec_start)]


def _schedule_argv(data_dir: Path, *, dest: Path | None = None, keep: int = 14) -> list[str]:
    return sched.state_authoritative_backup_argv(python=sys.executable, data_dir=data_dir, dest=dest, keep=keep)


@pytest.mark.requirement("LIBM-14780")
def test_schedule_argv_matches_backup_subcommand(machine_data_dir: Path) -> None:
    """[if] a schedule is rendered [then] argv matches backup CLI, [else stop]."""
    argv = _schedule_argv(machine_data_dir, keep=7)
    assert argv[1:6] == [
        "-m",
        "apps.shared.state_authoritative_backup",
        "backup",
        "--data-dir",
        str(machine_data_dir.resolve()),
    ]
    assert argv[-2:] == ["--keep", "7"]


@pytest.mark.requirement("LIBM-14780")
def test_rendered_systemd_backup_unit_runs_schedule_argv(machine_data_dir: Path) -> None:
    """[if] systemd units render [then] ExecStart round-trips schedule argv, [else stop]."""
    argv = _schedule_argv(machine_data_dir)
    names = sched.schedule_names(machine_data_dir)
    units = sched.render_schedule_units(kind="systemd", data_dir=machine_data_dir, argv=argv, names=names)
    (exec_start,) = _systemd_key(units[names["service"]], "ExecStart")
    assert _systemd_argv(exec_start) == argv
    assert _systemd_key(units[names["timer"]], "OnCalendar") == ["*-*-* 03:30:00 UTC"]


@pytest.mark.requirement("LIBM-14780")
def test_rendered_launchd_backup_plist_runs_schedule_argv(machine_data_dir: Path) -> None:
    """[if] launchd plist renders [then] ProgramArguments match argv at 03:30, [else stop]."""
    argv = _schedule_argv(machine_data_dir)
    names = sched.schedule_names(machine_data_dir)
    units = sched.render_schedule_units(kind="launchd", data_dir=machine_data_dir, argv=argv, names=names)
    plist = plistlib.loads(units[names["plist"]].encode())
    assert plist["ProgramArguments"] == argv
    assert plist["StartCalendarInterval"] == {"Hour": 3, "Minute": 30}


@pytest.mark.requirement("LIBM-14780")
def test_schedule_install_refuses_non_writable_data_dir(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] data-dir is not writable [then] install refuses with no registry, [else stop]."""
    bad = tmp_path / "readonly"
    bad.mkdir()
    os.chmod(bad, 0o555)
    with pytest.raises(StateAuthoritativeBackupError, match="not writable"):
        sched.schedule_install(
            data_dir=bad,
            python=sys.executable,
            kind="systemd",
            unit_dir=tmp_path / "units",
        )
    assert not sched.registry_path(bad).exists()


@pytest.mark.requirement("LIBM-14780")
def test_schedule_install_refuses_conflicting_prior_registry(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] registry exists with different argv [then] install refuses, [else stop]."""
    unit_dir = tmp_path / "units"
    sched.schedule_install(
        data_dir=machine_data_dir,
        python=sys.executable,
        keep=14,
        kind="systemd",
        unit_dir=unit_dir,
    )
    path = sched.registry_path(machine_data_dir)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["keep"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StateAuthoritativeBackupError, match="different schedule"):
        sched.schedule_install(
            data_dir=machine_data_dir,
            python=sys.executable,
            keep=14,
            kind="systemd",
            unit_dir=unit_dir,
        )


@pytest.mark.requirement("LIBM-14780")
def test_schedule_install_idempotent_same_plan(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] the same plan installs twice [then] one registry and same label, [else stop]."""
    unit_dir = tmp_path / "units"
    first = sched.schedule_install(
        data_dir=machine_data_dir,
        python=sys.executable,
        kind="systemd",
        unit_dir=unit_dir,
    )
    second = sched.schedule_install(
        data_dir=machine_data_dir,
        python=sys.executable,
        kind="systemd",
        unit_dir=unit_dir,
    )
    assert first.label == second.label
    assert sched.registry_path(machine_data_dir).is_file()


@pytest.mark.requirement("LIBM-14780")
def test_schedule_inspect_reports_installed_plan(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] install succeeds [then] inspect JSON matches argv, [else stop]."""
    unit_dir = tmp_path / "units"
    plan = sched.schedule_install(
        data_dir=machine_data_dir,
        python=sys.executable,
        kind="systemd",
        unit_dir=unit_dir,
    )
    report = sched.schedule_inspect(data_dir=machine_data_dir)
    assert report["argv"] == plan.argv
    assert report["label"] == plan.label
    assert report["kind"] == "systemd"


@pytest.mark.requirement("LIBM-14780")
def test_schedule_install_does_not_mutate_backup_semantics(
    machine_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] schedule-install runs [then] backup_state_db is not called, [else stop]."""

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("backup_state_db must not run during schedule-install")

    monkeypatch.setattr(sab, "backup_state_db", _forbidden)
    sched.schedule_install(
        data_dir=machine_data_dir,
        python=sys.executable,
        kind="systemd",
        unit_dir=tmp_path / "units",
    )


def _minimal_payload_python_tree(tmp_path: Path) -> tuple[Path, Path]:
    payload = tmp_path / "payload"
    runtime_python = payload / "runtime" / "bin" / "python3"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher = payload / "bin" / "opendj-python"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o755)
    return runtime_python, launcher


@pytest.mark.requirement("LIBM-14780")
def test_schedule_argv_resolves_payload_runtime_to_opendj_python(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] python is payload runtime/bin/python3 [then] argv uses opendj-python, [else stop]."""
    runtime_python, launcher = _minimal_payload_python_tree(tmp_path)
    plan = sched.schedule_install(
        data_dir=machine_data_dir,
        python=str(runtime_python),
        kind="systemd",
        unit_dir=tmp_path / "units",
        dry_run=True,
    )
    assert plan.argv[0] == str(launcher.resolve())
    assert plan.argv[1:5] == ["-m", "apps.shared.state_authoritative_backup", "backup", "--data-dir"]


@pytest.mark.requirement("LIBM-14780")
def test_schedule_argv_keeps_sys_executable_unchanged(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] dev sys.executable is used [then] argv[0] stays that interpreter, [else stop]."""
    plan = sched.schedule_install(
        data_dir=machine_data_dir,
        python=sys.executable,
        kind="systemd",
        unit_dir=tmp_path / "units",
        dry_run=True,
    )
    assert plan.argv[0] == sys.executable


@pytest.mark.requirement("LIBM-14780")
def test_rendered_systemd_execstart_uses_opendj_python_for_payload(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] payload python is scheduled [then] ExecStart starts with opendj-python, [else stop]."""
    runtime_python, launcher = _minimal_payload_python_tree(tmp_path)
    plan = sched.schedule_install(
        data_dir=machine_data_dir,
        python=str(runtime_python),
        kind="systemd",
        unit_dir=tmp_path / "units",
        dry_run=True,
    )
    names = sched.schedule_names(machine_data_dir)
    units = sched.render_schedule_units(kind="systemd", data_dir=machine_data_dir, argv=plan.argv, names=names)
    (exec_start,) = _systemd_key(units[names["service"]], "ExecStart")
    round_trip = _systemd_argv(exec_start)
    assert round_trip[0] == str(launcher.resolve())
    assert round_trip == plan.argv


@pytest.mark.requirement("LIBM-14780")
def test_schedule_launcher_refuses_missing_opendj_python(machine_data_dir: Path, tmp_path: Path) -> None:
    """[if] runtime python is given without launcher [then] install refuses, [else stop]."""
    payload = tmp_path / "payload"
    runtime_python = payload / "runtime" / "bin" / "python3"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("#!/bin/sh\n", encoding="utf-8")
    with pytest.raises(StateAuthoritativeBackupError, match="payload launcher not found"):
        sched.schedule_install(
            data_dir=machine_data_dir,
            python=str(runtime_python),
            kind="systemd",
            unit_dir=tmp_path / "units",
            dry_run=True,
        )
