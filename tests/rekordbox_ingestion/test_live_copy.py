"""Playlist pre-creation on a COPY of the real rekordbox DB set.

Skipped unless MDT_LIVE_LIBRARY=1 (CI has no library). Reads the live set only
to copy it; every write lands in tmp_path. Proves the write adds exactly the
folder + playlists rows (plus the USN counter) and appends exactly those XML
nodes, and that the backup is byte-identical to the source.

    MDT_LIVE_LIBRARY=1 .venv/bin/python -m pytest tests/rekordbox_ingestion/test_live_copy.py -v
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from apps.rekordbox_ingestion import playlists, status
from apps.shared import platform_paths
from apps.shared.hashing import sha256_file_hex

pytestmark = pytest.mark.skipif(
    os.environ.get("MDT_LIVE_LIBRARY") != "1", reason="needs the real rekordbox DB (MDT_LIVE_LIBRARY=1)"
)


def _all_rows(db_path: Path) -> dict[str, set[str]]:
    from pyrekordbox import Rekordbox6Database
    from sqlalchemy import text

    db = Rekordbox6Database(path=str(db_path), unlock=True)
    try:
        tables = [t for (t,) in db.session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))]
        return {t: {repr(tuple(r)) for r in db.session.execute(text(f'SELECT * FROM "{t}"'))} for t in tables}
    finally:
        db.close()
        db.engine.dispose()


def test_create_on_a_copy_changes_only_playlists_and_usn(tmp_path: Path, monkeypatch) -> None:
    live = platform_paths.REKORDBOX_LIVE_DB
    copy_dir = tmp_path / "rb"
    copy_dir.mkdir()
    with status.snapshot_db(live) as snap:
        for f in snap.parent.iterdir():
            shutil.copy2(f, copy_dir / f.name)
    db = copy_dir / live.name
    before_rows = _all_rows(db)
    # _all_rows opened + closed the copy, which checkpoints its WAL; re-hash so the
    # backup is compared with the files exactly as the write found them.
    source_hashes = {f.name: sha256_file_hex(f) for f in copy_dir.iterdir()}
    before_xml = playlists._xml_nodes(copy_dir / "masterPlaylists6.xml")
    monkeypatch.setenv("MDT_REKORDBOX_WRITEBACK_ENABLED", "1")

    backup, plan, ids = playlists.create_blank_playlists(
        db, "_ingest_test", ["probe-a", "probe-b"], backup_root=tmp_path / "bk"
    )

    after_rows = _all_rows(db)
    changed = {t for t in after_rows if after_rows[t] != before_rows.get(t)}
    assert changed == {"djmdPlaylist", "agentRegistry"}
    assert len(after_rows["djmdPlaylist"] - before_rows["djmdPlaylist"]) == 3
    assert not before_rows["djmdPlaylist"] - after_rows["djmdPlaylist"]
    assert set(ids) == {"probe-a", "probe-b"} and plan.to_create == ["probe-a", "probe-b"]
    after_xml = playlists._xml_nodes(copy_dir / "masterPlaylists6.xml")
    assert len(after_xml) == len(before_xml) + 3
    assert backup is not None and (backup.directory / "MANIFEST.sha256").is_file()
    # the backup is the set exactly as found, byte for byte, taken before any open
    assert {f.name: sha256_file_hex(f) for f in backup.files} == source_hashes
    # second run is a no-op on the DB rows
    again, _, _ = playlists.create_blank_playlists(db, "_ingest_test", ["probe-a"], backup_root=tmp_path / "bk")
    assert again is None
    assert _all_rows(db)["djmdPlaylist"] == after_rows["djmdPlaylist"]


def test_backup_keeps_the_wal_when_the_source_has_one(tmp_path: Path, monkeypatch) -> None:
    """Regression: the plan step used to open the live file first, folding the WAL away."""
    live = platform_paths.REKORDBOX_LIVE_DB
    if not Path(f"{live}-wal").exists():
        pytest.skip("live DB has no -wal right now; nothing to prove")
    copy_dir = tmp_path / "rb"
    copy_dir.mkdir()
    for suffix in ("", "-wal", "-shm"):
        src = Path(f"{live}{suffix}")
        if src.exists():
            shutil.copy2(src, copy_dir / src.name)
    shutil.copy2(live.parent / "masterPlaylists6.xml", copy_dir / "masterPlaylists6.xml")
    wal_hash = sha256_file_hex(copy_dir / f"{live.name}-wal")
    monkeypatch.setenv("MDT_REKORDBOX_WRITEBACK_ENABLED", "1")
    backup, _, _ = playlists.create_blank_playlists(
        copy_dir / live.name, "_ingest_test", ["probe-wal"], backup_root=tmp_path / "bk"
    )
    assert backup is not None
    assert sha256_file_hex(backup.directory / f"{live.name}-wal") == wal_hash
