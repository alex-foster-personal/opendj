"""Live Spotify listen history, scored against the local library.

The upsell pane: "you just played this and you do not own it - here is where
to buy it". Ownership is decided by the SAME matcher the playlist importer
uses, so a row that reads ``owned`` here would have matched on import too.

Nothing is persisted. A play is not an acquisition-queue entry; it becomes one
only when the operator imports a playlist. This module is read-only on both
sides: it reads Spotify and it reads state, and writes neither.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from .client import SpotifyClient, SpotifyTrack, parse_track_item
from .matcher_adapter import LocalTrack, load_local_tracks, match_spotify_tracks
from .purchase_links import suggested_sources

__all__ = ["RecentPlay", "build_recent_plays", "fetch_recent_plays", "parse_recent_items"]

OwnershipStatus = Literal["owned", "review", "unmatched"]


@dataclass(frozen=True)
class RecentPlay:
    """One Spotify play, plus what the local library thinks of it."""

    played_at: str
    spotify_uri: str
    isrc: str | None
    title: str
    artist: str
    album: str | None
    duration_ms: int | None
    ownership: OwnershipStatus
    confidence: float
    matched_stable_id: str | None
    suggested_sources: dict[str, str]


def _require(mapping: dict, key: str, context: str) -> object:
    if key not in mapping:
        raise ValueError(f"recently-played {context} has no {key!r}")
    return mapping[key]


def parse_recent_items(items: list[dict]) -> list[tuple[str, SpotifyTrack]]:
    """Turn raw ``/me/player/recently-played`` items into (played_at, track).

    The item shape is the playlist-item shape plus ``played_at``, so the track
    half goes through the importer's own parser and cannot drift from it.

    A malformed item is an error, never a silently dropped row: an under-count
    in this pane reads as "you have played less than you have".
    """
    parsed: list[tuple[str, SpotifyTrack]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"recently-played item {index} is not an object")
        played_at = _require(item, "played_at", f"item {index}")
        if not isinstance(played_at, str):
            raise ValueError(f"recently-played item {index} played_at is not a string")
        # Reject a timestamp we cannot order by rather than sorting on a string
        # that may not be ISO-8601 at all.
        datetime.fromisoformat(played_at.replace("Z", "+00:00"))

        track = parse_track_item(item)
        if track is None:
            raise ValueError(f"recently-played item {index} has no track")
        parsed.append((played_at, track))
    return parsed


def build_recent_plays(
    parsed: list[tuple[str, SpotifyTrack]],
    local_tracks: list[LocalTrack],
) -> list[RecentPlay]:
    """Score parsed plays against the local library, newest first."""
    result = match_spotify_tracks((track for _played_at, track in parsed), local_tracks)
    if len(result.pairs) != len(parsed):
        raise ValueError(
            f"matcher returned {len(result.pairs)} pairs for {len(parsed)} plays"
        )

    plays: list[RecentPlay] = []
    for (played_at, track), pair in zip(parsed, result.pairs, strict=True):
        ownership: OwnershipStatus = "owned" if pair.status == "matched" else pair.status
        plays.append(RecentPlay(
            played_at=played_at,
            spotify_uri=track.spotify_uri,
            isrc=track.isrc,
            title=track.title,
            artist=track.artists_joined,
            album=track.album or None,
            duration_ms=track.duration_ms or None,
            ownership=ownership,
            confidence=round(pair.confidence, 4),
            matched_stable_id=pair.target.stable_id if pair.target is not None else None,
            suggested_sources=suggested_sources(track.artists_joined, track.title),
        ))
    plays.sort(key=lambda play: play.played_at, reverse=True)
    return plays


def fetch_recent_plays(
    client: SpotifyClient,
    state_db_path: Path,
    *,
    limit: int = 50,
) -> list[RecentPlay]:
    """Fetch live plays and score them against the library at ``state_db_path``."""
    parsed = parse_recent_items(client.recently_played(limit=limit))
    conn = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True)
    try:
        local_tracks = load_local_tracks(conn)
    finally:
        conn.close()
    return build_recent_plays(parsed, local_tracks)
