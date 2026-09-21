"""SYNC-ONEWAY-01: open a Rekordbox DB through SQLCipher only when it is encrypted.

``pyrekordbox`` defaults ``unlock`` to True, which applies the Rekordbox key to
whatever file it is given. Handed an already-plain SQLite file, SQLCipher reads
ciphertext where the header is and fails with "file is not a database", which is
what broke the two rekordbox writeback acceptance tests on every runner that
actually has the fixture mounted.

Real temp files and the real pyrekordbox engine throughout. Nothing here is
mocked: the plain database is written by :mod:`sqlite3` and read back through a
real query, so a regression fails the way the live route failed.

[if] a plain Rekordbox DB is opened [then] it reads back without SQLCipher, [else stop].
[if] an encrypted Rekordbox DB is opened [then] it still goes through SQLCipher, [else stop].
[if] the header and the filename disagree [then] the header decides, [else stop].
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text

from apps.engine_core.setup import detect
from apps.shared import rekordbox_db


def _plain_sqlite(path: Path, rows: int = 5000) -> Path:
    """Write a real, multi-page, unencrypted SQLite database.

    The row count matters. A single-page file can be handed to SQLCipher and
    survive a bare ``connect()``, because nothing has read a page yet; the
    failure only surfaces on a query. Writing enough rows to need several pages
    keeps this test honest about the thing that actually broke.
    """
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


def _not_plain(path: Path) -> Path:
    """A file whose first bytes are not the SQLite magic, as ciphertext is."""
    path.write_bytes(os.urandom(4096))
    return path


def test_a_plain_rekordbox_database_opens_and_reads(tmp_path: Path) -> None:
    """[if] open_db gets a plain DB [then] the rows read back, [else stop]."""
    database = rekordbox_db.open_db(_plain_sqlite(tmp_path / "master.plain.db"))
    try:
        with database.engine.connect() as connection:
            count = connection.execute(
                text("SELECT count(*) FROM djmdContent")
            ).scalar()
    finally:
        database.close()
    assert count == 5000


def test_a_plain_database_is_not_routed_through_sqlcipher(tmp_path: Path) -> None:
    """[if] the file is plain [then] the driver is pysqlite, [else stop]."""
    database = rekordbox_db.open_db(_plain_sqlite(tmp_path / "master.plain.db", 16))
    try:
        assert database.engine.dialect.driver == "pysqlite"
    finally:
        database.close()


def test_an_encrypted_database_still_goes_through_sqlcipher(tmp_path: Path) -> None:
    """[if] the file is not plain [then] the driver is pysqlcipher, [else stop].

    The guard against over-correcting. Passing ``unlock=False`` unconditionally
    would make the test above pass and break every real user, whose live
    ``master.db`` and its ``master.db.copy`` byte copy are both encrypted.
    """
    database = rekordbox_db.open_db(_not_plain(tmp_path / "master.db"))
    try:
        assert database.engine.dialect.driver == "pysqlcipher"
    finally:
        database.close()


def test_sqlcipher_on_a_plain_file_is_the_failure_this_prevents(
    tmp_path: Path,
) -> None:
    """[if] a plain DB is forced through SQLCipher [then] it raises, [else stop].

    Pins the failure mode itself, so the two tests above are known to be
    guarding something real rather than passing because nothing can go wrong.
    """
    from pyrekordbox import Rekordbox6Database

    plain = _plain_sqlite(tmp_path / "master.plain.db")
    database = Rekordbox6Database(path=str(plain), unlock=True)
    try:
        with (
            pytest.raises(Exception, match="file is not a database"),
            database.engine.connect() as connection,
        ):
            connection.execute(text("SELECT count(*) FROM djmdContent"))
    finally:
        database.close()


def test_the_header_decides_not_the_filename(tmp_path: Path) -> None:
    """[if] name and header disagree [then] the header wins, [else stop]."""
    assert rekordbox_db.is_plain_sqlite(_plain_sqlite(tmp_path / "master.db", 4))
    assert not rekordbox_db.is_plain_sqlite(_not_plain(tmp_path / "master.plain.db"))


def test_a_missing_file_is_not_reported_plain(tmp_path: Path) -> None:
    """[if] the path does not exist [then] is_plain_sqlite is False, [else stop]."""
    assert not rekordbox_db.is_plain_sqlite(tmp_path / "absent.db")


def test_setup_detection_and_the_opener_cannot_disagree(tmp_path: Path) -> None:
    """[if] both header checks are asked [then] they answer the same, [else stop].

    ``detect.is_plain_sqlite`` decides "ready to ingest" and the opener decides
    "open without SQLCipher". Two header checks that could drift is how the
    repo ended up with three of them.
    """
    plain = _plain_sqlite(tmp_path / "plain.db", 4)
    encrypted = _not_plain(tmp_path / "encrypted.db")
    missing = tmp_path / "absent.db"
    for candidate in (plain, encrypted, missing):
        assert detect.is_plain_sqlite(candidate) == rekordbox_db.is_plain_sqlite(
            candidate
        )
