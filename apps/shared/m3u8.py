"""Shared M3U8 playlist line writer.

Output contract (from ``apps.sync.usb.playlist_writer``; confirmed by djay Pro,
Serato DJ, VLC)::

    #EXTM3U
    #EXTINF:<secs>,<Artist> - <Title>
    <location line written verbatim>
    ...
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class M3u8Entry:
    duration_s: int
    artist: str
    title: str
    location: str


def _flat(text: str) -> str:
    return text.replace("\n", " ").strip()


def m3u8_lines(entries: Iterable[M3u8Entry]) -> list[str]:
    lines: list[str] = ["#EXTM3U"]
    for entry in entries:
        artist = _flat(entry.artist)
        title = _flat(entry.title)
        extinf = f"#EXTINF:{entry.duration_s},{artist} - {title}"
        lines.append(extinf)
        lines.append(entry.location)
    return lines


def render_m3u8(entries: Iterable[M3u8Entry]) -> str:
    return "\n".join(m3u8_lines(entries)) + "\n"


__all__ = ["M3u8Entry", "m3u8_lines", "render_m3u8"]
