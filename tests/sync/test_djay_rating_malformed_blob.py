"""SYNC-12964: djay rating writes refuse malformed TSAF blobs and leave them byte-identical.

[if] a djay row is empty, not TSAF, or has no terminator [then] the write fails and bytes stay identical, [else stop].
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.sync.apply_ratings import _write_djay_rating
from apps.sync.djay_writer import is_patchable_tsaf_blob

pytestmark = pytest.mark.requirement("SYNC-12964")

_GOOD = b"TSAF" + b"\x00" * 12 + b"\x08uuid\x00" + b"\x0b\x05\x00\x00"


def _db_with_blob(tmp_path: Path, blob: bytes) -> Path:
    """Build a minimal djay database2 table holding one mediaItemUserData row."""
    db = tmp_path / "MediaLibrary.db"
    with sqlite3.connect(str(db)) as con:
        con.execute("CREATE TABLE database2 (collection TEXT, key TEXT, data BLOB)")
        con.execute("INSERT INTO database2 VALUES ('mediaItemUserData', 'u1', ?)", (blob,))
        con.commit()
    return db


def _stored(db: Path) -> bytes:
    """Read back the stored blob for the single test row."""
    with sqlite3.connect(str(db)) as con:
        return con.execute("SELECT data FROM database2 WHERE key = 'u1'").fetchone()[0]


@pytest.mark.parametrize(
    "blob",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"NOPE" + _GOOD[4:], id="no-tsaf-magic"),
        pytest.param(b"TSAF" + b"\x00" * 12 + b"\x08uuid\x00\x41", id="no-terminator"),
    ],
)
def test_malformed_blob_write_fails_and_bytes_unchanged(tmp_path: Path, blob: bytes) -> None:
    """A malformed blob fails the rating write and stays byte-identical.

    [if] the blob is empty, not TSAF, or lacks a terminator [then] the write returns False, row untouched, [else stop].
    """
    db = _db_with_blob(tmp_path, blob)
    assert is_patchable_tsaf_blob(blob) is False
    assert _write_djay_rating(db, "u1", 4) is False
    assert _stored(db) == blob


def test_well_formed_blob_still_accepts_rating(tmp_path: Path) -> None:
    """A well-formed TSAF blob with a terminator still takes the rating write.

    [if] the blob has TSAF magic and a ``0x0b 0x05`` tail chunk [then] the guard passes and bytes change, [else stop].
    """
    db = _db_with_blob(tmp_path, _GOOD)
    assert is_patchable_tsaf_blob(_GOOD) is True
    _write_djay_rating(db, "u1", 4)
    assert _stored(db) != _GOOD
