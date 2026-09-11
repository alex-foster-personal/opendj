"""Official SoundCloud OAuth API adapter for META-05.

The adapter uses only endpoints documented at
https://developers.soundcloud.com/docs/api/: ``GET /tracks`` for catalog
search and ``POST /playlists`` for the user-authorized target write.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from apps.spotify.client import SpotifyTrack

from .config import CFG


class SoundCloudError(RuntimeError):
    """SoundCloud configuration, wire, or HTTP failure."""


@dataclass(frozen=True)
class SoundCloudTrack:
    urn: str
    title: str
    artists: tuple[str, ...]
    duration_ms: int
    isrc: str | None
    permalink_url: str

    @property
    def numeric_id(self) -> int:
        prefix = "soundcloud:tracks:"
        if not self.urn.startswith(prefix) or not self.urn[len(prefix) :].isdigit():
            raise SoundCloudError(f"invalid SoundCloud track URN: {self.urn!r}")
        return int(self.urn[len(prefix) :])


def _required_string(row: dict[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SoundCloudError(f"SoundCloud track has invalid {field}: {value!r}")
    return value.strip()


def _artist(row: dict[str, Any]) -> str:
    metadata_artist = row.get("metadata_artist")
    if isinstance(metadata_artist, str) and metadata_artist.strip():
        return metadata_artist.strip()
    user = row.get("user")
    if isinstance(user, dict):
        username = user.get("username")
        if isinstance(username, str) and username.strip():
            return username.strip()
    raise SoundCloudError("SoundCloud track has neither metadata_artist nor user.username")


def parse_tracks_payload(payload: object) -> tuple[SoundCloudTrack, ...]:
    """Parse the documented linked-partitioning ``GET /tracks`` response."""
    if not isinstance(payload, dict) or not isinstance(payload.get("collection"), list):
        raise SoundCloudError("SoundCloud search response must contain collection[]")
    tracks: list[SoundCloudTrack] = []
    for item in payload["collection"]:
        if not isinstance(item, dict) or item.get("kind") != "track":
            raise SoundCloudError("SoundCloud collection item is not a track")
        duration = item.get("duration")
        if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
            raise SoundCloudError(f"SoundCloud track has invalid duration: {duration!r}")
        isrc = item.get("isrc")
        if isrc is not None and (not isinstance(isrc, str) or not isrc.strip()):
            raise SoundCloudError(f"SoundCloud track has invalid isrc: {isrc!r}")
        tracks.append(
            SoundCloudTrack(
                urn=_required_string(item, "urn"),
                title=_required_string(item, "title"),
                artists=(_artist(item),),
                duration_ms=duration,
                isrc=isrc.strip().upper() if isinstance(isrc, str) else None,
                permalink_url=_required_string(item, "permalink_url"),
            )
        )
    return tuple(tracks)


def build_playlist_payload(
    title: str,
    tracks: tuple[SoundCloudTrack, ...],
    *,
    sharing: str,
) -> dict[str, object]:
    """Build the documented JSON body while preserving source membership order."""
    if sharing not in {"private", "public"}:
        raise SoundCloudError(f"invalid SoundCloud sharing value: {sharing!r}")
    if not title.strip() or not tracks:
        raise SoundCloudError("SoundCloud playlist needs a title and at least one track")
    return {
        "playlist": {
            "title": title.strip(),
            "description": "Transferred by Open DJ",
            "sharing": sharing,
            "tracks": [{"id": track.numeric_id} for track in tracks],
        }
    }


class SoundCloudClient:
    """Synchronous client for search and one atomic playlist creation request."""

    def __init__(self, access_token: str) -> None:
        if not access_token.strip():
            raise SoundCloudError("SoundCloud access token must not be empty")
        timeout = httpx.Timeout(
            connect=CFG.connect_timeout_s,
            read=CFG.read_timeout_s,
            write=30.0,
            pool=10.0,
        )
        self._client = httpx.Client(
            base_url=CFG.soundcloud_api_base,
            timeout=timeout,
            headers={
                "Accept": "application/json; charset=utf-8",
                "Authorization": f"OAuth {access_token}",
            },
        )

    @classmethod
    def from_env(cls) -> SoundCloudClient:
        return cls(CFG.require_secret(CFG.soundcloud_token_env))

    def close(self) -> None:
        self._client.close()

    def search_tracks(self, source: SpotifyTrack) -> tuple[SoundCloudTrack, ...]:
        query = " ".join((*source.artists, source.title)).strip()
        if not query:
            raise SoundCloudError(f"Spotify track {source.spotify_uri!r} has no search text")
        response = self._client.get(
            "/tracks",
            params={
                "q": query,
                "access": "playable",
                "limit": CFG.search_limit,
                "linked_partitioning": "true",
            },
        )
        self._raise_for_status(response, "track search")
        return parse_tracks_payload(response.json())

    def create_playlist(
        self,
        title: str,
        tracks: tuple[SoundCloudTrack, ...],
        *,
        sharing: str,
    ) -> str:
        response = self._client.post(
            "/playlists",
            json=build_playlist_payload(title, tracks, sharing=sharing),
        )
        self._raise_for_status(response, "playlist creation")
        body = response.json()
        if not isinstance(body, dict):
            raise SoundCloudError("SoundCloud playlist response must be an object")
        urn = body.get("urn")
        if isinstance(urn, str) and urn.strip():
            return urn.strip()
        playlist_id = body.get("id")
        if isinstance(playlist_id, int) and not isinstance(playlist_id, bool):
            return f"soundcloud:playlists:{playlist_id}"
        raise SoundCloudError("SoundCloud playlist response has no id or urn")

    @staticmethod
    def _raise_for_status(response: httpx.Response, operation: str) -> None:
        if 200 <= response.status_code < 300:
            return
        raise SoundCloudError(f"SoundCloud {operation} returned HTTP {response.status_code}")
