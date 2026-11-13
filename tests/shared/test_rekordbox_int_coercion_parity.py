"""Parity tests: both Rekordbox readers share coerce_optional's integer rules (issue #13136)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.shared import rekordbox_db
from apps.shared.state.ingest import rekordbox as ingest_rekordbox


def _content_row(value: Any) -> SimpleNamespace:
    """Build a minimal DjmdContent double with every integer column set to ``value``."""
    return SimpleNamespace(
        ID=1,
        Title="t",
        Artist=None,
        Album=None,
        Genre=None,
        Key=None,
        FolderPath="/Users/dev/Music/t.mp3",
        ISRC=None,
        DateCreated=None,
        BPM=value,
        Length=value,
        Rating=value,
        FileSize=value,
    )


def _db(value: Any) -> SimpleNamespace:
    """Wrap one content row in a ``get_content()`` double both readers accept."""
    return SimpleNamespace(get_content=lambda: [_content_row(value)])


@pytest.mark.parametrize(
    ("value", "expected"),
    [("12800", 12800), ("not-a-number", None), ("12.7", None), ("", None), (None, None)],
)
def test_both_readers_coerce_integer_columns_identically(value: Any, expected: int | None) -> None:
    """[if] two Rekordbox readers coerce the same value [then] they agree, [else stop]."""
    track = next(rekordbox_db.iter_tracks(_db(value)))
    row = next(ingest_rekordbox._rb_rows(_db(value)))

    assert track.rating == row["rating"] == expected
    assert track.file_size == row["file_size"] == expected
    expected_bpm = expected / 100.0 if expected is not None else None
    assert track.bpm == row["bpm"] == expected_bpm
    assert track.duration_s == (float(expected) if expected is not None else None)
    assert row["duration_ms"] == (expected * 1000 if expected is not None else None)
