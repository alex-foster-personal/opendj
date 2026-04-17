"""Tests for TSAF PLAYLIST_TYPE_LEAF startup validation (F3-3, SYNC-02).

See `.planning/DIAGNOSE-v1-2026-04-17.md` finding F3-3: the
``PLAYLIST_TYPE_LEAF = 0x01`` byte in :mod:`apps.sync.playlist_tsaf` is a
best-guess that must be validated against a live djay/Rekordbox DB before
any live apply. These tests cover the four paths of
:func:`apps.sync.playlist_tsaf.validate_leaf_type_byte`:

* no DB path (warn + return ``None``)
* explicit skip (return ``None``, no scan)
* matching observed byte (return the byte)
* mismatching observed byte (raise :class:`TSAFLeafTypeMismatch`)
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.sync import playlist_tsaf as ptsaf


def _write_fixture_db(path: Path, type_byte: int) -> None:
    """Write a minimal sqlite fixture with one non-root playlist blob.

    The blob only needs the ``0x2d <type_byte> 0x08 type 0x00`` marker
    pattern that :func:`_discover_leaf_type_from_db` scans for.
    """
    blob = (
        b"TSAF\x03\x03\x01\x00" + b"\x00" * 8 +
        b"\x2b\x08ADCMediaItemPlaylist\x00" +
        b"\x2d" + bytes([type_byte & 0xFF]) + b"\x08type\x00"
    )
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE djayMediaLibrary_playlist (data BLOB)")
        conn.execute(
            "INSERT INTO djayMediaLibrary_playlist (data) VALUES (?)", (blob,)
        )
        conn.commit()


@pytest.mark.requirement("SYNC-02")
def test_validate_no_db_path_logs_and_returns_none(caplog) -> None:
    caplog.set_level("WARNING")
    assert ptsaf.validate_leaf_type_byte(None) is None
    assert any("TSAF validation" in r.message for r in caplog.records)


@pytest.mark.requirement("SYNC-02")
def test_validate_skip_flag_bypasses_scan(tmp_path: Path) -> None:
    # Even with a mismatching DB on disk, skip=True returns None without raising.
    db = tmp_path / "master.db"
    _write_fixture_db(db, type_byte=(ptsaf.PLAYLIST_TYPE_LEAF ^ 0xFF) & 0xFF)
    assert ptsaf.validate_leaf_type_byte(db, skip=True) is None


@pytest.mark.requirement("SYNC-02")
def test_validate_skip_env_var_bypasses_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "master.db"
    _write_fixture_db(db, type_byte=(ptsaf.PLAYLIST_TYPE_LEAF ^ 0xFF) & 0xFF)
    monkeypatch.setenv("MDJ_SKIP_TSAF_VALIDATION", "1")
    assert ptsaf.validate_leaf_type_byte(db) is None


@pytest.mark.requirement("SYNC-02")
def test_validate_agreeing_db_returns_observed_byte(tmp_path: Path) -> None:
    db = tmp_path / "master.db"
    _write_fixture_db(db, type_byte=ptsaf.PLAYLIST_TYPE_LEAF)
    assert ptsaf.validate_leaf_type_byte(db) == ptsaf.PLAYLIST_TYPE_LEAF


@pytest.mark.requirement("SYNC-02")
def test_validate_disagreeing_db_raises(tmp_path: Path) -> None:
    db = tmp_path / "master.db"
    wrong = (ptsaf.PLAYLIST_TYPE_LEAF + 7) & 0xFF
    _write_fixture_db(db, type_byte=wrong)
    with pytest.raises(ptsaf.TSAFLeafTypeMismatch) as exc:
        ptsaf.validate_leaf_type_byte(db)
    assert f"0x{wrong:02x}" in str(exc.value)
    assert "--skip-tsaf-validation" in str(exc.value)


@pytest.mark.requirement("SYNC-02")
def test_validate_missing_db_file_returns_none(tmp_path: Path, caplog) -> None:
    caplog.set_level("WARNING")
    missing = tmp_path / "does-not-exist.db"
    assert ptsaf.validate_leaf_type_byte(missing) is None


@pytest.mark.requirement("SYNC-02")
def test_validate_db_without_playlist_tables_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
    assert ptsaf.validate_leaf_type_byte(db) is None
