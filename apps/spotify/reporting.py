"""Per-import reporting artifacts.

Emits under ``data/spotify/import-<playlist_id>-<timestamp>/``:

* ``matches.csv`` -- every track + decision (matched / review / unmatched).
* ``to-acquire.csv`` -- unmatched subset + 5 deep-link search URLs.
* ``to-acquire.md`` -- human-readable review sheet.
* ``summary.md`` -- top-level stats.

Atomic writes (tmp + os.replace).
"""
from __future__ import annotations

import csv
import io
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from apps.shared.paths import DATA_DIR

from .acquisition import (
    AcquisitionEntry,
    build_acquisition_entries,
    render_markdown_file,
)
from .client import SpotifyPlaylist
from .matcher_adapter import MatchedPair, MatchResult

__all__ = [
    "ReportPaths",
    "build_report_dir",
    "write_matches_csv",
    "write_to_acquire_csv",
    "write_to_acquire_md",
    "write_summary_md",
    "write_all_reports",
]


_MATCHES_COLS: tuple[str, ...] = (
    "position", "spotify_uri", "isrc", "title", "artist", "album",
    "duration_ms", "status", "confidence", "signals",
    "local_stable_id", "local_title", "local_artist",
)

_TO_ACQUIRE_COLS: tuple[str, ...] = (
    "playlist_id", "playlist_position", "spotify_uri", "isrc",
    "title", "artist", "album", "duration_ms", "confidence",
    "suggested_source_beatport", "suggested_source_bandcamp",
    "suggested_source_qobuz", "suggested_source_apple",
    "suggested_source_discogs", "notes", "status",
)


@dataclass(frozen=True)
class ReportPaths:
    root: Path
    matches_csv: Path
    to_acquire_csv: Path
    to_acquire_md: Path
    summary_md: Path


def build_report_dir(
    playlist_id: str,
    *,
    root: Path | None = None,
    timestamp: datetime | None = None,
) -> ReportPaths:
    base = Path(root) if root is not None else DATA_DIR / "spotify"
    ts = (timestamp or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    dest = base / f"import-{playlist_id}-{ts}"
    dest.mkdir(parents=True, exist_ok=True)
    return ReportPaths(
        root=dest,
        matches_csv=dest / "matches.csv",
        to_acquire_csv=dest / "to-acquire.csv",
        to_acquire_md=dest / "to-acquire.md",
        summary_md=dest / "summary.md",
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # P09-F02: pin UTF-8 so non-ASCII track/artist names survive locales
    # whose default encoding is not UTF-8 (e.g. legacy cp1252).
    tmp.write_text(text, newline="", encoding="utf-8")
    os.replace(tmp, path)


def write_matches_csv(pairs: Iterable[MatchedPair], path: Path) -> Path:
    sio = io.StringIO(newline="")
    writer = csv.DictWriter(sio, fieldnames=list(_MATCHES_COLS))
    writer.writeheader()
    for idx, pair in enumerate(pairs):
        src = pair.source
        tgt = pair.target
        writer.writerow(
            {
                "position": idx,
                "spotify_uri": src.spotify_uri,
                "isrc": src.isrc or "",
                "title": src.title,
                "artist": src.artists_joined,
                "album": src.album,
                "duration_ms": src.duration_ms,
                "status": pair.status,
                "confidence": f"{pair.confidence:.4f}",
                "signals": "|".join(pair.signals),
                "local_stable_id": tgt.stable_id if tgt else "",
                "local_title": tgt.title if tgt else "",
                "local_artist": tgt.artists_joined if tgt else "",
            }
        )
    _atomic_write_text(path, sio.getvalue())
    return path


def write_to_acquire_csv(
    playlist: SpotifyPlaylist,
    entries: Iterable[AcquisitionEntry],
    path: Path,
) -> Path:
    sio = io.StringIO(newline="")
    writer = csv.DictWriter(sio, fieldnames=list(_TO_ACQUIRE_COLS))
    writer.writeheader()
    for e in entries:
        writer.writerow(
            {
                "playlist_id": playlist.id,
                "playlist_position": e.position,
                "spotify_uri": e.spotify_uri,
                "isrc": e.isrc or "",
                "title": e.title,
                "artist": e.artist,
                "album": e.album,
                "duration_ms": e.duration_ms,
                "confidence": f"{e.confidence:.4f}",
                "suggested_source_beatport": e.sources["beatport"],
                "suggested_source_bandcamp": e.sources["bandcamp"],
                "suggested_source_qobuz": e.sources["qobuz"],
                "suggested_source_apple": e.sources["apple_music"],
                "suggested_source_discogs": e.sources["discogs"],
                "notes": "",
                "status": "pending",
            }
        )
    _atomic_write_text(path, sio.getvalue())
    return path


def write_to_acquire_md(
    playlist: SpotifyPlaylist,
    entries: list[AcquisitionEntry],
    path: Path,
    *,
    include_timestamp: bool = True,
) -> Path:
    return render_markdown_file(
        playlist, entries, path, include_timestamp=include_timestamp
    )


def write_summary_md(
    playlist: SpotifyPlaylist,
    result: MatchResult,
    path: Path,
    *,
    include_timestamp: bool = True,
    runtime_seconds: float | None = None,
    live: bool = False,
) -> Path:
    lines: list[str] = []
    lines.append(f"# Spotify import summary -- {playlist.name}")
    lines.append("")
    lines.append(f"- Playlist id: `{playlist.id}`")
    lines.append(f"- Owner: {playlist.owner}")
    lines.append(f"- Snapshot: `{playlist.snapshot_id}`")
    lines.append(f"- Mode: **{'LIVE' if live else 'dry-run'}**")
    if include_timestamp:
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
        lines.append(f"- Generated: {ts}")
    if runtime_seconds is not None:
        lines.append(f"- Runtime: {runtime_seconds:.2f}s")
    lines.append("")
    lines.append("## Counts")
    lines.append("")
    lines.append(f"- Total tracks: {len(result.pairs)}")
    lines.append(f"- Matched: {len(result.matched)}")
    lines.append(f"- Review: {len(result.review)}")
    lines.append(f"- Unmatched: {len(result.unmatched)}")
    lines.append(f"- Match rate: {result.match_rate:.1%}")
    lines.append("")
    _atomic_write_text(path, "\n".join(lines))
    return path


def write_all_reports(
    playlist: SpotifyPlaylist,
    result: MatchResult,
    paths: ReportPaths,
    *,
    runtime_seconds: float | None = None,
    live: bool = False,
    include_timestamp: bool = True,
) -> ReportPaths:
    entries = build_acquisition_entries(result.pairs)
    write_matches_csv(result.pairs, paths.matches_csv)
    write_to_acquire_csv(playlist, entries, paths.to_acquire_csv)
    write_to_acquire_md(
        playlist, entries, paths.to_acquire_md, include_timestamp=include_timestamp
    )
    write_summary_md(
        playlist, result, paths.summary_md,
        include_timestamp=include_timestamp,
        runtime_seconds=runtime_seconds, live=live,
    )
    return paths
