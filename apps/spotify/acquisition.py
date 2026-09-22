"""Acquisition queue -- Markdown review sheet for unmatched tracks.

Deterministic output (same input -> byte-identical output when
``include_timestamp=False``). Sort: confidence ASC then title ASC so
the DJ tackles the clearest gaps first.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote_plus

from .client import SpotifyPlaylist
from .matcher_adapter import MatchedPair

__all__ = [
    "SOURCE_TEMPLATES",
    "AcquisitionEntry",
    "build_acquisition_entries",
    "render_markdown",
    "render_markdown_file",
    "format_duration",
]


#: (display_name, key, url_template). Stable order matches the CSV cols.
SOURCE_TEMPLATES: tuple[tuple[str, str, str], ...] = (
    ("Beatport", "beatport", "https://www.beatport.com/search?q={q}"),
    ("Bandcamp", "bandcamp", "https://bandcamp.com/search?q={q}&item_type=t"),
    ("Qobuz", "qobuz", "https://www.qobuz.com/us-en/search?q={q}"),
    ("Apple Music", "apple_music", "https://music.apple.com/us/search?term={q}"),
    ("Discogs", "discogs", "https://www.discogs.com/search?q={q}&type=release"),
)


@dataclass(frozen=True)
class AcquisitionEntry:
    position: int
    title: str
    artist: str
    album: str
    duration_ms: int
    isrc: str | None
    spotify_uri: str
    confidence: float
    sources: dict[str, str]


def format_duration(ms: int) -> str:
    if ms <= 0:
        return "0:00"
    total_s = ms // 1000
    m, s = divmod(total_s, 60)
    return f"{m}:{s:02d}"


def _search_urls(artist: str, title: str) -> dict[str, str]:
    q = quote_plus(f"{artist} {title}".strip())
    return {key: template.format(q=q) for _name, key, template in SOURCE_TEMPLATES}


def build_acquisition_entries(
    pairs: Iterable[MatchedPair],
) -> list[AcquisitionEntry]:
    """Convert matcher pairs (review + unmatched) into sortable entries."""
    entries: list[AcquisitionEntry] = []
    for idx, pair in enumerate(pairs):
        if pair.status == "matched":
            continue
        src = pair.source
        entries.append(
            AcquisitionEntry(
                position=idx,
                title=src.title,
                artist=src.artists_joined,
                album=src.album,
                duration_ms=src.duration_ms,
                isrc=src.isrc,
                spotify_uri=src.spotify_uri,
                confidence=pair.confidence,
                sources=_search_urls(src.artists_joined, src.title),
            )
        )
    entries.sort(key=lambda e: (e.confidence, e.title.casefold()))
    return entries


def _spotify_web_url(uri: str) -> str:
    if uri.startswith("spotify:track:"):
        tid = uri.split(":", 2)[2]
        return f"https://open.spotify.com/track/{tid}"
    return uri  # pragma: no cover


def render_markdown(
    playlist: SpotifyPlaylist,
    entries: list[AcquisitionEntry],
    *,
    include_timestamp: bool = True,
    include_footer: bool = True,
) -> str:
    lines: list[str] = []
    lines.append(f"# Acquisition queue -- {playlist.name}")
    lines.append("")
    lines.append(f"- Spotify playlist: `{playlist.id}`")
    lines.append(f"- Snapshot: `{playlist.snapshot_id}`")
    lines.append(f"- Tracks to acquire: **{len(entries)}**")
    if include_timestamp:
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
        lines.append(f"- Generated: {ts}")
    lines.append("")

    if not entries:
        lines.append(
            "> Nothing to acquire -- every track matched cleanly. "
            "Nice library."
        )
        lines.append("")
        return "\n".join(lines)

    for entry in entries:
        lines.append(f"## {entry.artist} -- {entry.title}")
        lines.append("")
        lines.append(f"- Album: {entry.album or '_unknown_'}")
        lines.append(f"- Duration: {format_duration(entry.duration_ms)}")
        lines.append(f"- ISRC: `{entry.isrc}`" if entry.isrc else "- ISRC: _missing_")
        lines.append(
            f"- Spotify: [{entry.spotify_uri}]"
            f"({_spotify_web_url(entry.spotify_uri)})"
        )
        lines.append(f"- Match confidence: {entry.confidence:.2f}")
        lines.append("")
        lines.append("Search on:")
        for display_name, key, _ in SOURCE_TEMPLATES:
            lines.append(f"- [{display_name}]({entry.sources[key]})")
        lines.append("")
        lines.append("- [ ] purchased")
        lines.append("- [ ] downloaded")
        lines.append("- [ ] imported into library")
        lines.append("")
        lines.append("<!-- notes: -->")
        lines.append("")

    if include_footer:
        lines.append("---")
        lines.append("")
        lines.append(
            "Re-run matching after acquiring: "
            "`make spotify-rematch PLAYLIST_ID=" + playlist.id + "`"
        )
        lines.append("")
    return "\n".join(lines)


def render_markdown_file(
    playlist: SpotifyPlaylist,
    entries: list[AcquisitionEntry],
    out_path: Path,
    *,
    include_timestamp: bool = True,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = render_markdown(
        playlist, entries, include_timestamp=include_timestamp
    )
    # P09-F02: pin the text encoding so non-ASCII track/artist names are
    # not corrupted under locales where the default encoding is not UTF-8.
    out_path.write_text(text, encoding="utf-8")
    return out_path
