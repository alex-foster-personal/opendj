"""M3U8 playlist emitter.

One file per profile playlist under ``<drive_root>/Playlists/<name>.m3u8``.
Paths are drive-relative with forward slashes. UTF-8 encoding, no BOM.

Output contract (confirmed by djay Pro, Serato DJ, VLC)::

    #EXTM3U
    #EXTINF:<secs>,<Artist> - <Title>
    <drive-relative path with forward slashes>
    ...
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from apps.sync.usb.layout import dst_relpath, sanitise_segment
from apps.sync.usb.profile import Profile
from apps.sync.usb.state import CanonicalTrack

PLAYLISTS_DIRNAME = "Playlists"


def _playlist_filename(name: str) -> str:
    """Return a filesystem-safe ``.m3u8`` filename for a playlist name."""
    return sanitise_segment(name) + ".m3u8"


def _m3u8_lines_for(
    *,
    tracks: Iterable[CanonicalTrack],
    profile: Profile,
) -> list[str]:
    lines: list[str] = ["#EXTM3U"]
    for idx, t in enumerate(tracks, start=1):
        duration_secs = 0
        if t.duration_ms is not None:
            duration_secs = max(0, round(t.duration_ms / 1000))
        title_display = (t.title or t.source_path.stem).replace("\n", " ").strip()
        artist_display = (t.artist or "").replace("\n", " ").strip()
        extinf = f"#EXTINF:{duration_secs},{artist_display} - {title_display}"
        rel = dst_relpath(
            layout=profile.layout,
            format_=profile.format,
            playlist=t.playlist,
            track_index=idx,
            artist=t.artist,
            album=t.album,
            title=t.title,
            src=t.source_path,
        )
        # Drive-relative with forward slashes; use a leading "../" hop-free
        # path because the m3u8 itself lives under /Playlists/.
        rel_from_playlists = f"../{rel.as_posix()}"
        lines.append(extinf)
        lines.append(rel_from_playlists)
    return lines


def write_m3u8s(
    *,
    profile: Profile,
    tracks_by_playlist: dict[str, list[CanonicalTrack]],
    drive_root: Path,
    only: set[str] | None = None,
) -> list[Path]:
    """Write one .m3u8 per profile playlist. Returns the list of paths.

    Does nothing (returns ``[]``) when ``profile.playlist_files == "none"``.

    When ``only`` is provided, restricts emission to the named subset of
    ``profile.playlists``. This is used by cautious-mode apply so that
    playlists whose tracks were not touched in the current run are not
    silently rewritten (Codex finding P10-F01).
    """
    if profile.playlist_files == "none":
        return []

    out_dir = drive_root / PLAYLISTS_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for playlist_name in profile.playlists:
        if only is not None and playlist_name not in only:
            continue
        tracks = tracks_by_playlist.get(playlist_name, [])
        lines = _m3u8_lines_for(tracks=tracks, profile=profile)
        path = out_dir / _playlist_filename(playlist_name)
        # Atomic-ish: write to .part then replace.
        part = path.with_name(path.name + ".part")
        part.write_text("\n".join(lines) + "\n", encoding="utf-8")
        part.replace(path)
        written.append(path)
    return written


__all__ = [
    "PLAYLISTS_DIRNAME",
    "write_m3u8s",
]
