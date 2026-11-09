"""Playlist text export (rekordbox Export a playlist to a file, .txt; mimicked).

A playlist becomes a UTF-8, tab-separated track list: one header row, then one row per track
in playlist order. A tab, carriage return or newline inside a field becomes a space, so every
row has the same column count. Pure: the route and the CLI share it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

TEXT_EXPORT_COLUMNS: tuple[str, ...] = ("#", "Title", "Artist", "BPM", "Key", "Time")
TEXT_EXPORT_MEDIA_TYPE = "text/plain; charset=utf-8"
_FIELD_BREAKS = re.compile(r"[\t\r\n]")


@dataclass(frozen=True)
class TextExportTrack:
    """The fields one exported row reads; a missing value exports as an empty cell."""

    title: str | None
    artist: str | None
    bpm: float | None
    key: str | None
    duration_ms: int | None


def _clean(value: str | None) -> str:
    return "" if value is None else _FIELD_BREAKS.sub(" ", value)


def _time(duration_ms: int | None) -> str:
    if duration_ms is None:
        return ""
    total_s = round(duration_ms / 1000)
    return f"{total_s // 60}:{total_s % 60:02d}"


def playlist_text_export(tracks: Iterable[TextExportTrack]) -> str:
    """The whole export: header row, then one row per track, each ending in a newline."""
    rows = ["\t".join(TEXT_EXPORT_COLUMNS)]
    for number, track in enumerate(tracks, start=1):
        rows.append(
            "\t".join(
                (
                    str(number),
                    _clean(track.title),
                    _clean(track.artist),
                    "" if track.bpm is None else f"{track.bpm:.2f}",
                    _clean(track.key),
                    _time(track.duration_ms),
                )
            )
        )
    return "\n".join(rows) + "\n"


def export_filename(playlist_name: str) -> str:
    """A download filename from the playlist name, safe for a Content-Disposition header."""
    stem = re.sub(r"[^A-Za-z0-9 ._-]+", "_", playlist_name).strip(" ._") or "playlist"
    return f"{stem}.txt"
