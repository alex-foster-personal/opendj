"""SYNC-ONEWAY-01: back up a plain Rekordbox DB, not just an encrypted one.

`_online_backup_unlocked_rekordbox` read the SQLCipher key off the engine URL
and refused outright when there was not one. A plain database is opened without
SQLCipher, so its URL has no key, so the writeback path raised
``rekordbox: unlocked SQLCipher engine has no key`` before it could take the
backup the write is gated on -- no backup, no write, for anyone whose
``master.db`` is plain.

Real SQLite files and a real SQLAlchemy engine. The encrypted half needs a real
Rekordbox key and lives in the vendor acceptance tests; what is pinned here is
that the plain branch produces a readable, complete copy and that the
encryption check still runs in both directions.

[if] a plain DB is backed up [then] the copy is readable and complete, [else stop].
[if] the copy does not match the source's encryption [then] it is refused, [else stop].
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from apps.smartlists.rb_writer import (
    _assert_backup_matches_source,
    _copy_open_database,
)


def _library(path: Path, rows: int = 500) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE djmdContent (ID INTEGER PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO djmdContent (ID) VALUES (?)", [(n,) for n in range(rows)]
        )
        connection.commit()
    finally:
        connection.close()
    return path


def test_a_plain_database_is_copied_while_it_is_open(tmp_path: Path) -> None:
    """[if] the source is plain [then] the copy carries every row, [else stop]."""
    source = _library(tmp_path / "master.plain.db")
    destination = tmp_path / "backup.db"
    engine = create_engine(f"sqlite:///{source}")
    raw = engine.raw_connection()
    try:
        _copy_open_database(raw.driver_connection, destination, "bk", None)
    finally:
        raw.close()
        engine.dispose()

    assert destination.is_file()
    with sqlite3.connect(destination) as copy:
        assert copy.execute("SELECT count(*) FROM djmdContent").fetchone()[0] == 500


def test_the_copy_includes_commits_still_sitting_in_the_wal(tmp_path: Path) -> None:
    """[if] a commit is still in the WAL [then] the backup has it, [else stop].

    This is the difference between an online backup and copying the file, and
    it is not academic: SQLite in WAL mode keeps committed transactions in
    ``-wal`` until a checkpoint, so copying ``master.db`` alone silently loses
    whatever was committed most recently -- which is exactly the data a
    rollback would be reached for.

    Measured both ways while writing this: ``VACUUM INTO`` finds the row,
    ``shutil.copy2`` of the same source returns 0. An earlier version of this
    test wrote through the connection in the default journal mode, where both
    approaches pass, so it proved nothing.
    """
    source = _library(tmp_path / "master.plain.db", rows=10)
    destination = tmp_path / "backup.db"
    engine = create_engine(f"sqlite:///{source}")
    raw = engine.raw_connection()
    try:
        driver = raw.driver_connection
        assert driver.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        driver.execute("INSERT INTO djmdContent (ID) VALUES (9999)")
        driver.commit()
        assert (tmp_path / "master.plain.db-wal").is_file(), (
            "the commit must still be in the WAL for this test to mean anything"
        )
        _copy_open_database(driver, destination, "bk", None)
    finally:
        raw.close()
        engine.dispose()

    with sqlite3.connect(destination) as copy:
        assert copy.execute(
            "SELECT count(*) FROM djmdContent WHERE ID = 9999"
        ).fetchone()[0] == 1


def test_a_plain_source_must_produce_a_readable_copy(tmp_path: Path) -> None:
    """[if] a plain backup is unreadable [then] it is refused, [else stop]."""
    corrupt = tmp_path / "backup.db"
    corrupt.write_bytes(b"\x00\x01\x02not a database at all" * 40)
    with pytest.raises(RuntimeError, match="not readable"):
        _assert_backup_matches_source(corrupt, encrypted=False)


def test_an_encrypted_source_must_not_produce_a_readable_copy(
    tmp_path: Path,
) -> None:
    """[if] an encrypted backup reads as plain [then] it is refused, [else stop].

    The direction that protects the library rather than the rollback: a
    readable copy of an encrypted source means the key was never applied.
    """
    leaked = _library(tmp_path / "backup.db", rows=4)
    with pytest.raises(RuntimeError, match="not encrypted"):
        _assert_backup_matches_source(leaked, encrypted=True)


def test_a_matching_pair_is_accepted_in_both_directions(tmp_path: Path) -> None:
    """[if] the copy matches the source [then] nothing is raised, [else stop].

    The control. Without it the two checks above would pass just as happily
    against a function that refused everything.
    """
    _assert_backup_matches_source(_library(tmp_path / "plain.db", rows=4), encrypted=False)
    ciphertext = tmp_path / "encrypted.db"
    ciphertext.write_bytes(b"\x8a\x1f\xd3\x77" * 1024)
    _assert_backup_matches_source(ciphertext, encrypted=True)
