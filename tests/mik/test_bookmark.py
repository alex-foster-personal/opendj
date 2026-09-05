"""Bookmark-blob parser tests.

The headline regression: an ASCII scrape truncates at the first non-ASCII byte
and produced a FALSE ZERO match on the first audit pass. ``test_ascii_scrape_
would_truncate_but_parser_does_not`` encodes that failure directly, so nobody
"simplifies" the parser back into a scrape.

[if] a bookmark blob is well-formed, non-ASCII, or malformed [then] it parses/raises, [else stop].
"""
from __future__ import annotations

import pytest

from apps.mik.bookmark import BookmarkParseError, parse_bookmark_path

from .conftest import make_bookmark

pytestmark = pytest.mark.requirement("META-01")


def test_round_trips_an_ascii_path() -> None:
    path = "/Users/dev/Music/Convert/Artist/Album/01 Track.mp3"
    assert parse_bookmark_path(make_bookmark(path)).path == path


def test_ascii_scrape_would_truncate_but_parser_does_not() -> None:
    # Real library paths carry accents, U+2014 character-free but non-ASCII all the same.
    path = "/Users/dev/Music/Café del Mar/Ibiza/Señor Coconut.mp3"
    blob = make_bookmark(path)
    assert parse_bookmark_path(blob).path == path
    # An ASCII scrape of the same blob loses the non-ASCII components, which is
    # exactly how the first pass reported zero matches.
    scraped = blob.decode("ascii", "ignore")
    assert "Café" not in scraped
    assert "Señor" not in scraped


def test_components_and_volume_are_reported() -> None:
    decoded = parse_bookmark_path(make_bookmark("/Users/dev/a.mp3"))
    assert decoded.components == ("Users", "dev", "a.mp3")
    assert decoded.volume_path == "/"
    assert decoded.volume_name == "Macintosh HD"


def test_bad_magic_raises() -> None:
    blob = bytearray(make_bookmark("/Users/dev/a.mp3"))
    blob[0:4] = b"junk"
    with pytest.raises(BookmarkParseError, match="bad magic"):
        parse_bookmark_path(bytes(blob))


def test_truncated_blob_raises_rather_than_guessing() -> None:
    blob = make_bookmark("/Users/dev/a.mp3")
    with pytest.raises(BookmarkParseError, match="truncated"):
        parse_bookmark_path(blob[:-8])


def test_too_short_raises() -> None:
    with pytest.raises(BookmarkParseError, match="too short"):
        parse_bookmark_path(b"book")


def test_non_bytes_raises() -> None:
    with pytest.raises(BookmarkParseError, match="expected bytes"):
        parse_bookmark_path("/Users/dev/a.mp3")  # type: ignore[arg-type]
