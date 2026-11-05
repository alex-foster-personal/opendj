"""Shared Spotify test object builders (tests only)."""

from __future__ import annotations

from apps.spotify.client import SpotifyPlaylist, SpotifyTrack
from apps.spotify.matcher_adapter import LocalTrack, MatchedPair


def make_playlist(
    *,
    playlist_id: str = "pl123",
    name: str = "Pop 2026",
    snapshot_id: str = "snap-1",
    owner: str = "dev3",
    description: str = "",
    tracks: tuple[SpotifyTrack, ...] = (),
) -> SpotifyPlaylist:
    return SpotifyPlaylist(
        id=playlist_id,
        name=name,
        snapshot_id=snapshot_id,
        owner=owner,
        description=description,
        tracks=tracks,
    )


def make_spotify_track(
    spotify_id: str,
    *,
    title: str = "Title",
    isrc: str | None = "USABC2500001",
    artists: tuple[str, ...] = ("Artist",),
    album: str = "Album",
    duration_ms: int = 200000,
    spotify_uri: str | None = None,
) -> SpotifyTrack:
    return SpotifyTrack(
        spotify_id=spotify_id,
        spotify_uri=spotify_uri or f"spotify:track:{spotify_id}",
        isrc=isrc,
        title=title,
        artists=artists,
        album=album,
        duration_ms=duration_ms,
        is_local=False,
    )


def make_local_track(
    stable_id: str,
    *,
    isrc: str | None = "USABC2500001",
    title: str = "Invented Title",
    artists: tuple[str, ...] = ("Artist",),
    duration_ms: int = 200000,
) -> LocalTrack:
    return LocalTrack(
        stable_id=stable_id,
        isrc=isrc,
        title=title,
        artists=artists,
        duration_ms=duration_ms,
    )


def make_matched_pair(
    *,
    title: str = "Mystery",
    artist: str = "Who",
    confidence: float = 0.0,
    status: str = "unmatched",
    isrc: str | None = None,
    spotify_uri: str = "spotify:track:abc",
    duration_ms: int = 200000,
    album: str = "Alb",
    spotify_id: str = "abc",
) -> MatchedPair:
    src = SpotifyTrack(
        spotify_id=spotify_id,
        spotify_uri=spotify_uri,
        isrc=isrc,
        title=title,
        artists=(artist,),
        album=album,
        duration_ms=duration_ms,
        is_local=False,
    )
    return MatchedPair(
        source=src,
        target=None,
        confidence=confidence,
        signals=(),
        status=status,
    )
