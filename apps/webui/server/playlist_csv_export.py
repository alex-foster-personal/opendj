"""Playlist CSV export (LIBM-114, CSV leaf): a set list a spreadsheet opens as-is.

One header row, then one row per track in playlist order. RFC 4180 quoting through the stdlib
``csv`` module (CRLF row ends, a comma, quote or newline inside a field is quoted, not
stripped). Encoded UTF-8 WITH a byte order mark, mimicked from what Excel needs: it opens a
BOM-less UTF-8 CSV as ANSI and mangles accents. Pure: the route and the CLI share it.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass

from .playlist_text_export import export_filename as _text_export_filename

CSV_EXPORT_COLUMNS: tuple[str, ...] = (
    "position",
    "title",
    "artist",
    "album",
    "genre",
    "bpm",
    "key",
    "duration_s",
    "rating",
    "comment",
    "path",
)
CSV_EXPORT_MEDIA_TYPE = "text/csv; charset=utf-8"
UTF8_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class CsvExportTrack:
    """The fields one exported row reads; a missing value exports as an empty cell."""

    title: str | None
    artist: str | None
    album: str | None
    genre: str | None
    bpm: float | None
    key: str | None
    duration_ms: int | None
    rating: int | None
    comment: str | None
    path: str | None


def _cell(value: object | None) -> str:
    return "" if value is None else str(value)


def _row(position: int, track: CsvExportTrack) -> list[str]:
    return [
        str(position),
        _cell(track.title),
        _cell(track.artist),
        _cell(track.album),
        _cell(track.genre),
        "" if track.bpm is None else f"{track.bpm:.2f}",
        _cell(track.key),
        "" if track.duration_ms is None else f"{track.duration_ms / 1000:.3f}",
        _cell(track.rating),
        _cell(track.comment),
        _cell(track.path),
    ]


def playlist_csv_export(tracks: Iterable[CsvExportTrack]) -> bytes:
    """The whole export as bytes: UTF-8 BOM, header row, then one row per track."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(CSV_EXPORT_COLUMNS)
    for position, track in enumerate(tracks, start=1):
        writer.writerow(_row(position, track))
    return UTF8_BOM + buffer.getvalue().encode("utf-8")


def csv_export_filename(playlist_name: str) -> str:
    """A ``<playlist>.csv`` download filename, safe for a Content-Disposition header."""
    return _text_export_filename(playlist_name).removesuffix(".txt") + ".csv"
