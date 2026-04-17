"""In-memory fake of the ``spotipy.Spotify`` surface we depend on.

Implements only ``playlist`` + ``playlist_items`` since those are all
``apps.spotify.client.SpotifyClient`` calls. Supports multi-page
pagination and a single-shot 401 injection for retry testing.
"""
from __future__ import annotations


class FakeSpotipy:
    def __init__(
        self,
        *,
        meta: dict,
        items_pages: list[list[dict]],
        raise_401_once_on: str | None = None,
    ) -> None:
        self.meta = dict(meta)
        self.items_pages = [list(p) for p in items_pages]
        self._raise_401_once_on = raise_401_once_on
        self._raised = False
        self.call_log: list[tuple[str, dict]] = []

    def playlist(self, playlist_id: str, fields: str | None = None) -> dict:
        self.call_log.append(("playlist", {"id": playlist_id, "fields": fields}))
        self._maybe_raise("playlist")
        meta = dict(self.meta)
        meta["id"] = playlist_id
        return meta

    def playlist_items(
        self,
        playlist_id: str,
        limit: int = 100,
        offset: int = 0,
        additional_types: tuple[str, ...] = ("track",),
    ) -> dict:
        self.call_log.append(
            ("playlist_items", {"id": playlist_id, "limit": limit, "offset": offset}),
        )
        self._maybe_raise("playlist_items")
        page_index = offset // limit
        items = self.items_pages[page_index] if page_index < len(self.items_pages) else []
        has_next = page_index + 1 < len(self.items_pages) and items
        return {
            "items": items,
            "next": "http://next" if has_next else None,
            "limit": limit,
            "offset": offset,
        }

    def _maybe_raise(self, op: str) -> None:
        if self._raised:
            return
        if self._raise_401_once_on == op:
            self._raised = True
            raise FakeSpotifyException(http_status=401, msg="token expired")


class FakeSpotifyException(Exception):
    def __init__(self, *, http_status: int, msg: str = "") -> None:
        super().__init__(msg)
        self.http_status = http_status


def make_track(
    *,
    spotify_id: str | None = "trk000",
    uri: str | None = None,
    isrc: str | None = "USABC2500001",
    name: str = "Test Title",
    artists: list[str] | None = None,
    album: str = "Test Album",
    duration_ms: int = 200000,
    is_local: bool = False,
) -> dict:
    if artists is None:
        artists = ["Test Artist"]
    return {
        "track": {
            "id": spotify_id,
            "uri": uri or (f"spotify:track:{spotify_id}" if spotify_id else ""),
            "external_ids": {"isrc": isrc} if isrc else {},
            "name": name,
            "artists": [{"name": a} for a in artists],
            "album": {"name": album},
            "duration_ms": duration_ms,
            "is_local": is_local,
        }
    }


def make_meta(
    *,
    playlist_id: str = "37i9dQZF1DXcBWIGoYBM5M",
    name: str = "Test Playlist",
    snapshot_id: str = "snap-1",
    owner_name: str = "dev3",
    description: str = "a test playlist",
) -> dict:
    return {
        "id": playlist_id,
        "name": name,
        "snapshot_id": snapshot_id,
        "owner": {"id": "dev3", "display_name": owner_name},
        "description": description,
    }
