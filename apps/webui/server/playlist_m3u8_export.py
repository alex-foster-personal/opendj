"""Playlist M3U8 export (LIBORG-9748): a playlist file other players load, as rekordbox's Export a playlist writes.

Extended M3U, UTF-8 (no BOM), LF line ends, mimicked from rekordbox: ``#EXTM3U``, then per track
``#EXTINF:<seconds>,<artist> - <title>`` and its absolute path, in playlist order. A track with no
local file (streaming, or the file is gone) has no line a player could open, so it is skipped and
counted in one ``#`` comment line after the header. Pure: the route resolves which paths are local.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .playlist_text_export import export_filename as _text_export_filename

M3U8_EXPORT_MEDIA_TYPE = "audio/x-mpegurl; charset=utf-8"
M3U8_HEADER = "#EXTM3U"


@dataclass(frozen=True)
class M3u8ExportTrack:
    """One playlist entry; ``local_path`` is None when the track has no local file to point at."""

    title: str | None
    artist: str | None
    duration_ms: int | None
    local_path: str | None


@dataclass(frozen=True)
class M3u8Export:
    """The export bytes plus how many entries were skipped for having no local file."""

    body: bytes
    skipped: int


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _extinf(track: M3u8ExportTrack, local_path: str) -> str:
    seconds = -1 if track.duration_ms is None else round(track.duration_ms / 1000)
    title = _one_line(track.title or "") or Path(local_path).stem
    artist = _one_line(track.artist or "")
    return f"#EXTINF:{seconds},{artist} - {title}" if artist else f"#EXTINF:{seconds},{title}"


def playlist_m3u8_export(tracks: Iterable[M3u8ExportTrack]) -> M3u8Export:
    """The whole export: header, a skipped-count comment when any were skipped, then EXTINF and path per track."""
    entries: list[str] = []
    skipped = 0
    for track in tracks:
        if track.local_path is None:
            skipped += 1
            continue
        entries += [_extinf(track, track.local_path), track.local_path]
    lines = [M3U8_HEADER]
    if skipped:
        lines.append(f"# skipped {skipped} track(s) with no local file (streaming or missing)")
    return M3u8Export(body=("\n".join([*lines, *entries]) + "\n").encode("utf-8"), skipped=skipped)


def local_track_path(file_path: str | None) -> str | None:
    """``file_path`` when it is an absolute path to a file on this host, else None (streaming URI or missing file)."""
    if not file_path:
        return None
    path = Path(file_path)
    return file_path if path.is_absolute() and path.is_file() else None


def m3u8_export_filename(playlist_name: str) -> str:
    """A ``<playlist>.m3u8`` download filename, safe for a Content-Disposition header."""
    return _text_export_filename(playlist_name).removesuffix(".txt") + ".m3u8"
