"""SYNC-12984: djay rating writes refuse ambiguous duplicate rating fields.

[if] a TSAF blob holds more than one candidate rating field [then] no rating write happens, [else stop].
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest

from apps.shared.djay_db import (
    AmbiguousRatingFieldError,
    extract_schema_indexed_uint8,
    locate_rating_value_offset,
)
from apps.sync.apply_ratings import _write_djay_rating
from apps.sync.djay_writer import patch_rating

pytestmark = pytest.mark.requirement("SYNC-12984")

_TRAIL = b"\x0b\x05\x00\x00"


def _schema_blob(body: bytes) -> bytes:
    """Build a TSAF blob whose schema is ``[uuid, rating, colorIndex]`` (rating idx 1)."""
    names = b"\x08uuid\x00\x08rating\x00\x08colorIndex\x00"
    head = b"TSAF" + b"\x00" * 4 + struct.pack("<I", 3) + b"\x00" * 4
    return head + b"\x08uuid\x00" + b"\x0b" + struct.pack("<I", 3) + names + body + _TRAIL


def _legacy_blob(body: bytes) -> bytes:
    """Build a schema-less TSAF blob with only a legacy tail rating marker."""
    return b"TSAF" + b"\x00" * 12 + body + _TRAIL


def test_unique_schema_indexed_rating_is_patched_not_the_tail_lookalike() -> None:
    """[if] one schema-indexed rating field [then] only its byte changes, colorIndex stays, [else stop]."""
    # colorIndex (idx 2) sits first, so a naive tail scan would take it as the rating.
    blob = _schema_blob(b"\x0f\x03\x05\x02" + b"\x0f\x04\x05\x01")
    new = patch_rating(blob, 2)
    assert extract_schema_indexed_uint8(new, "rating") == 2
    assert extract_schema_indexed_uint8(new, "colorIndex") == 3
    assert len(new) == len(blob)


def test_two_schema_indexed_rating_candidates_refuse_the_write() -> None:
    """[if] two conflicting schema-indexed rating candidates [then] the patch raises, [else stop]."""
    blob = _schema_blob(b"\x0f\x04\x05\x01" + b"\x0f\x02\x05\x01")
    with pytest.raises(AmbiguousRatingFieldError):
        locate_rating_value_offset(blob)
    with pytest.raises(AmbiguousRatingFieldError):
        patch_rating(blob, 5)


def test_legacy_blob_with_one_tail_marker_is_patched() -> None:
    """[if] a legacy blob has exactly one tail rating marker [then] its value is replaced, [else stop]."""
    blob = _legacy_blob(b"\x0f\x03\x08rating\x00")
    offset = locate_rating_value_offset(blob)
    assert offset is not None and blob[offset] == 3
    new = patch_rating(blob, 5)
    assert new[offset] == 5
    assert len(new) == len(blob)


def test_legacy_blob_with_two_tail_markers_refuses_the_write() -> None:
    """[if] a legacy blob has two tail rating lookalikes [then] the patch raises, [else stop]."""
    blob = _legacy_blob(b"\x0f\x03\x08rating\x00\x0f\x02\x08playCount\x00")
    with pytest.raises(AmbiguousRatingFieldError):
        patch_rating(blob, 4)


def test_apply_ratings_skips_ambiguous_blob_without_writing(tmp_path: Path) -> None:
    """[if] the stored blob is ambiguous [then] the write returns False and leaves the row, [else stop]."""
    blob = _schema_blob(b"\x0f\x04\x05\x01" + b"\x0f\x02\x05\x01")
    db = tmp_path / "MediaLibrary.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE database2 (collection TEXT, key TEXT, data BLOB)")
        con.execute("INSERT INTO database2 VALUES ('mediaItemUserData', 'u1', ?)", (blob,))
    assert _write_djay_rating(db, "u1", 5) is False
    with sqlite3.connect(db) as con:
        (stored,) = con.execute("SELECT data FROM database2 WHERE key = 'u1'").fetchone()
    assert stored == blob
