"""SQLite backup regressions for live writeback targets."""
from __future__ import annotations

import sqlite3

from apps.smartlists import writeback_backup


def test_online_backup_includes_committed_wal_rows(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(writeback_backup, "WRITEBACK_BACKUP_DIR", tmp_path / "backups")
    live = tmp_path / "live.db"
    source = sqlite3.connect(live)
    try:
        source.execute("CREATE TABLE membership (stable_id TEXT)")
        source.commit()
        assert source.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        source.execute("INSERT INTO membership VALUES ('from-wal')")
        source.commit()
        backup_id = writeback_backup.online_backup(source, "djay")
    finally:
        source.close()
    with sqlite3.connect(writeback_backup.backup_path("djay", backup_id)) as restored:
        assert restored.execute("SELECT stable_id FROM membership").fetchall() == [("from-wal",)]


def test_separate_reader_backup_completes_while_writer_holds_immediate_lock(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(writeback_backup, "WRITEBACK_BACKUP_DIR", tmp_path / "backups")
    live = tmp_path / "live.db"
    writer = sqlite3.connect(live)
    try:
        writer.execute("CREATE TABLE membership (stable_id TEXT)")
        writer.execute("INSERT INTO membership VALUES ('a')")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        with sqlite3.connect(f"file:{live}?mode=ro", uri=True) as reader:
            backup_id = writeback_backup.online_backup(reader, "rekordbox")
        writer.rollback()
    finally:
        writer.close()
    with sqlite3.connect(writeback_backup.backup_path("rekordbox", backup_id)) as copied:
        assert copied.execute("SELECT stable_id FROM membership").fetchall() == [("a",)]


# backup ids are uuid4().hex and are joined into a path, so the format is
# enforced now; a short label like "r1" is no longer a legal id.
REVERSAL_ID = "a" * 32


def test_reversal_metadata_is_bound_to_one_exact_vendor_target_and_revision(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(writeback_backup, "WRITEBACK_BACKUP_DIR", tmp_path / "backups")
    writeback_backup.WRITEBACK_BACKUP_DIR.mkdir()
    target = tmp_path / "live.db"
    target.touch()
    writeback_backup.write_reversal("djay", REVERSAL_ID, target, "playlist-a", ["a"], ["native-a"], "post-revision")
    assert writeback_backup.read_reversal("djay", REVERSAL_ID, target, "playlist-a", "post-revision") == (["a"], ["native-a"])
    try:
        other = tmp_path / "other.db"
        other.touch()
        writeback_backup.read_reversal("djay", REVERSAL_ID, other, "playlist-a", "post-revision")
    except RuntimeError as exc:
        assert "does not bind" in str(exc)
    else:
        raise AssertionError("reversal replay against another target was accepted")
