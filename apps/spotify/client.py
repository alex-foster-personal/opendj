"""Spotify Web API client wrapper for Phase 9.

Narrow surface:
* OAuth PKCE via ``spotipy.oauth2.SpotifyPKCE`` (token at
  ``~/.music-dj-tools/spotify-token.json``, outside the repo).
* Typed ``SpotifyPlaylist`` + ``SpotifyTrack`` dataclasses.
* 24 h JSON cache at ``(private path)/<playlist_id>.json``.
* Pagination for ``GET /v1/playlists/{id}/tracks``.
* One retry on 401 to absorb stale-token after sleep.

Tests swap the underlying spotipy via ``spotipy_client`` ctor arg.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from apps.shared.paths import DATA_DIR

__all__ = [
    "CACHE_DIR",
    "CONNECT_WEB_PLAYBACK_SCOPES",
    "DEFAULT_REDIRECT_URI",
    "DEFAULT_SCOPES",
    "SCOPE_STREAMING",
    "SCOPE_USER_MODIFY_PLAYBACK_STATE",
    "SCOPE_USER_READ_CURRENTLY_PLAYING",
    "SCOPE_USER_READ_PLAYBACK_STATE",
    "TOKEN_CACHE_PATH",
    "MissingCredentialsError",
    "ReauthorizationRequired",
    "SpotifyClient",
    "SpotifyClientProtocol",
    "SpotifyPlaylist",
    "SpotifyTrack",
    "parse_playlist_payload",
    "parse_track_item",
]

_CACHE_TTL_SECONDS: int = 24 * 60 * 60

# Spotify playlist IDs are base62; accept alphanumerics plus dash to stay
# compatible with legacy/test fixtures while still blocking traversal
# primitives (``/``, ``\``, ``..``, leading ``.``, absolute paths). See
# (private path) finding 2.
_SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{0,63}$")


def _validate_playlist_id(playlist_id: str) -> str:
    if not isinstance(playlist_id, str) or not _SPOTIFY_ID_RE.fullmatch(playlist_id):
        raise ValueError(f"invalid Spotify playlist_id {playlist_id!r}: expected alphanumeric + dash, length 1..64")
    return playlist_id


def _track_id_from_spotify_uri(uri: str) -> str | None:
    """Return the bare track id from ``spotify:track:<id>``, else ``None``."""
    if not uri.startswith("spotify:track:"):
        return None
    tid = uri.split(":", 2)[2]
    return tid or None


TOKEN_CACHE_PATH: Path = Path("~/.music-dj-tools/spotify-token.json").expanduser()
CACHE_DIR: Path = DATA_DIR / "spotify" / "cache"

DEFAULT_REDIRECT_URI: str = "http://127.0.0.1:8888/callback"
# Adding a scope invalidates the cached token: spotipy notices the scope set
# changed and re-runs the PKCE browser flow ONCE. That is deliberate and
# visible, not something to work around by keeping two token caches.
DEFAULT_SCOPES: str = "playlist-read-private playlist-read-collaborative user-read-recently-played"

# Connect / Web Playback SDK single-stream CUE (SPOT sprint, parent #305). Login
# and DEFAULT_SCOPES do not request these yet; a later change extends scopes and
# forces one deliberate re-auth.
SCOPE_STREAMING: str = "streaming"
SCOPE_USER_READ_PLAYBACK_STATE: str = "user-read-playback-state"
SCOPE_USER_MODIFY_PLAYBACK_STATE: str = "user-modify-playback-state"
SCOPE_USER_READ_CURRENTLY_PLAYING: str = "user-read-currently-playing"
CONNECT_WEB_PLAYBACK_SCOPES: str = " ".join(
    (
        SCOPE_STREAMING,
        SCOPE_USER_READ_PLAYBACK_STATE,
        SCOPE_USER_MODIFY_PLAYBACK_STATE,
        SCOPE_USER_READ_CURRENTLY_PLAYING,
    )
)


@dataclass(frozen=True)
class SpotifyTrack:
    """One track from a Spotify playlist.

    ``isrc`` is ``None`` when Spotify didn't return one (common for
    user-uploaded local files). ``is_local=True`` marks user uploads.
    """

    spotify_id: str | None
    spotify_uri: str
    isrc: str | None
    title: str
    artists: tuple[str, ...]
    album: str
    duration_ms: int
    is_local: bool

    @property
    def artists_joined(self) -> str:
        return ", ".join(self.artists)


@dataclass(frozen=True)
class SpotifyPlaylist:
    """Fully-fetched playlist including every track.

    ``snapshot_id`` is the Spotify-native ETag used for idempotent
    re-imports (CONTEXT D5).
    """

    id: str
    name: str
    snapshot_id: str
    owner: str
    description: str
    tracks: tuple[SpotifyTrack, ...] = field(default_factory=tuple)

    @property
    def track_count(self) -> int:
        return len(self.tracks)


class MissingCredentialsError(RuntimeError):
    """Raised when ``SPOTIFY_CLIENT_ID`` is missing from the environment."""


class ReauthorizationRequired(RuntimeError):
    """Raised when a non-interactive caller has no usable cached token.

    Distinct from ``MissingCredentialsError``: the app IS configured, the
    user simply has to complete a browser sign-in that only a terminal can
    drive. The two states want different words in the UI.
    """


class SpotifyClientProtocol(Protocol):
    """Minimal shape we depend on from ``spotipy.Spotify``."""

    def playlist(self, playlist_id: str, fields: str | None = None) -> dict: ...  # pragma: no cover -- protocol

    def playlist_items(
        self,
        playlist_id: str,
        limit: int = 100,
        offset: int = 0,
        additional_types: tuple[str, ...] = ("track",),
    ) -> dict: ...  # pragma: no cover -- protocol

    def current_user_recently_played(self, limit: int = 50) -> dict: ...  # pragma: no cover -- protocol

    def me(self) -> dict: ...  # pragma: no cover -- protocol


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _cache_path(playlist_id: str, cache_dir: Path) -> Path:
    # Defense in depth: refuse to build a cache path for an unvalidated id.
    _validate_playlist_id(playlist_id)
    return cache_dir / f"{playlist_id}.json"


def _cache_fresh(path: Path, ttl: int) -> bool:
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < ttl


def _write_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_cache(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Payload -> typed
# ---------------------------------------------------------------------------


def parse_track_item(item: dict) -> SpotifyTrack | None:
    """Convert one ``playlist_items.items[i]`` payload entry.

    Returns ``None`` for null / removed entries (Spotify does that).
    """
    track = item.get("track") if item else None
    if track is None:
        return None

    is_local = bool(track.get("is_local"))
    spotify_id = track.get("id")
    uri = track.get("uri") or ""
    ex = track.get("external_ids") or {}
    isrc = ex.get("isrc") if isinstance(ex, dict) else None
    title = track.get("name") or ""
    artists = tuple(a.get("name", "") for a in (track.get("artists") or []) if isinstance(a, dict))
    album_obj = track.get("album") or {}
    album = album_obj.get("name", "") if isinstance(album_obj, dict) else ""
    duration_ms = int(track.get("duration_ms") or 0)

    return SpotifyTrack(
        spotify_id=spotify_id,
        spotify_uri=uri,
        isrc=isrc or None,
        title=title,
        artists=artists,
        album=album,
        duration_ms=duration_ms,
        is_local=is_local,
    )


def _parse_playlist_payload(payload: dict) -> SpotifyPlaylist:
    items: Iterable[dict] = payload.get("_items", [])
    tracks = tuple(t for t in (parse_track_item(it) for it in items) if t is not None)
    owner_obj = payload.get("owner") or {}
    owner = owner_obj.get("display_name") or owner_obj.get("id") or ""
    return SpotifyPlaylist(
        id=payload.get("id", ""),
        name=payload.get("name", ""),
        snapshot_id=payload.get("snapshot_id", ""),
        owner=owner,
        description=payload.get("description", "") or "",
        tracks=tracks,
    )


def parse_playlist_payload(payload: dict) -> SpotifyPlaylist:
    """Parse a captured or fetched Spotify playlist through the wire contract."""
    return _parse_playlist_payload(payload)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SpotifyClient:
    """Thin wrapper around ``spotipy.Spotify`` for Phase 9."""

    def __init__(
        self,
        spotipy_client: SpotifyClientProtocol,
        *,
        cache_dir: Path = CACHE_DIR,
        cache_ttl: int = _CACHE_TTL_SECONDS,
    ) -> None:
        self._sp = spotipy_client
        self._cache_dir = cache_dir
        self._cache_ttl = cache_ttl

    @classmethod
    def from_env(
        cls,
        *,
        cache_dir: Path = CACHE_DIR,
        cache_ttl: int = _CACHE_TTL_SECONDS,
        interactive: bool = True,
    ) -> SpotifyClient:
        """Build a real client using ``SPOTIFY_CLIENT_ID`` from the env.

        ``interactive`` is the difference between a terminal and a daemon.
        In a terminal (the default) an unusable token opens a browser and
        waits on the loopback callback, which is the whole point of the CLI.

        A SERVER must never do that: it would open a browser on a host nobody
        is sitting at and block the HTTP worker until the callback times out.
        ``interactive=False`` therefore refuses up front with
        ``ReauthorizationRequired`` rather than starting a flow that cannot
        complete. This fires on a SCOPE CHANGE too, not only on expiry:
        spotipy treats a token cached under a narrower scope set as no token
        at all, so adding a scope makes every daemon call raise this until
        someone completes the browser sign-in once from a terminal.
        """
        client_id = os.environ.get("SPOTIFY_CLIENT_ID")
        if not client_id:
            raise MissingCredentialsError(
                "SPOTIFY_CLIENT_ID not set. Wrap the command with `doppler run -- ...` or export the env var manually."
            )
        import spotipy
        from spotipy.oauth2 import SpotifyPKCE

        TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        auth = SpotifyPKCE(
            client_id=client_id,
            redirect_uri=DEFAULT_REDIRECT_URI,
            scope=DEFAULT_SCOPES,
            cache_path=str(TOKEN_CACHE_PATH),
            open_browser=interactive,
        )
        if not interactive and auth.get_cached_token() is None:
            raise ReauthorizationRequired(
                "No usable cached Spotify token for scopes "
                f"'{DEFAULT_SCOPES}'. Expected after a scope change. Run "
                "`doppler run -p construct -c dev_af -- python -m apps.spotify "
                "recent --limit 1` once in a terminal to complete the browser "
                "sign-in, then retry."
            )
        sp = spotipy.Spotify(auth_manager=auth)
        return cls(sp, cache_dir=cache_dir, cache_ttl=cache_ttl)

    @classmethod
    def from_saved(
        cls,
        *,
        cache_dir: Path = CACHE_DIR,
        cache_ttl: int = _CACHE_TTL_SECONDS,
        interactive: bool = True,
        client_id_path: Path | None = None,
        token_path: Path | None = None,
    ) -> SpotifyClient:
        """Build a client from the saved Client ID file or ``SPOTIFY_CLIENT_ID``.

        Same PKCE and ``interactive`` semantics as :meth:`from_env`; only the
        client-id source differs (``resolve_client_id`` instead of env-only).
        """
        from apps.spotify.account import resolve_client_id

        client_id = resolve_client_id(client_id_path=client_id_path)
        if client_id is None:
            raise MissingCredentialsError(
                "No Spotify Client ID is saved and SPOTIFY_CLIENT_ID is not set. "
                "Paste your app's Client ID in the Spotify source panel, or export "
                "SPOTIFY_CLIENT_ID for the daemon."
            )
        import spotipy
        from spotipy.oauth2 import SpotifyPKCE

        token_cache = token_path or TOKEN_CACHE_PATH
        token_cache.parent.mkdir(parents=True, exist_ok=True)
        auth = SpotifyPKCE(
            client_id=client_id,
            redirect_uri=DEFAULT_REDIRECT_URI,
            scope=DEFAULT_SCOPES,
            cache_path=str(token_cache),
            open_browser=interactive,
        )
        if not interactive and auth.get_cached_token() is None:
            raise ReauthorizationRequired(
                "No usable cached Spotify token for scopes "
                f"'{DEFAULT_SCOPES}'. Expected after a scope change. Run "
                "`doppler run -p construct -c dev_af -- python -m apps.spotify "
                "recent --limit 1` once in a terminal to complete the browser "
                "sign-in, then retry."
            )
        sp = spotipy.Spotify(auth_manager=auth)
        return cls(sp, cache_dir=cache_dir, cache_ttl=cache_ttl)

    def fetch_playlist(
        self,
        playlist_id: str,
        *,
        use_cache: bool = True,
    ) -> SpotifyPlaylist:
        """Fetch playlist metadata + every track; consult 24 h cache first."""
        _validate_playlist_id(playlist_id)
        path = _cache_path(playlist_id, self._cache_dir)
        if use_cache and _cache_fresh(path, self._cache_ttl):
            return _parse_playlist_payload(_read_cache(path))
        payload = self._fetch_uncached(playlist_id)
        _write_cache(path, payload)
        return _parse_playlist_payload(payload)

    def _fetch_uncached(self, playlist_id: str) -> dict:
        meta = self._call(
            lambda: self._sp.playlist(
                playlist_id,
                fields=("id,name,snapshot_id,owner(id,display_name),description,tracks(total)"),
            ),
        )
        items: list[dict] = []
        offset = 0
        limit = 100
        while True:
            page = self._call(
                lambda off=offset: self._sp.playlist_items(
                    playlist_id,
                    limit=limit,
                    offset=off,
                    additional_types=("track",),
                ),
            )
            page_items = page.get("items") or []
            items.extend(page_items)
            if not page_items:
                break
            if page.get("next") is None:
                break
            offset += len(page_items)

        meta["_items"] = items
        return meta

    def current_user(self) -> dict:
        """The authenticated Spotify profile. Raises if the token is unusable."""
        return self._call(self._sp.me)

    def recently_played(self, *, limit: int = 50) -> list[dict]:
        """Raw ``/me/player/recently-played`` items, newest first.

        Never cached: the whole point of the pane is what was just played.
        Spotify caps this endpoint at 50 items and at roughly the last 50
        plays, so there is no pagination loop to write.
        """
        if not 1 <= limit <= 50:
            raise ValueError(f"limit must be 1..50, got {limit}")
        payload = self._call(lambda: self._sp.current_user_recently_played(limit=limit))
        items = payload.get("items")
        if not isinstance(items, list):
            raise ValueError("recently-played payload has no 'items' list")
        return items

    def _call(self, thunk) -> Any:
        """One-retry on 401; spotipy refreshes tokens automatically."""
        try:
            return thunk()
        except Exception as exc:
            if _is_http_status(exc, 401):
                return thunk()
            raise


def _is_http_status(exc: BaseException, status: int) -> bool:
    code = getattr(exc, "http_status", None) or getattr(exc, "status", None)
    return code == status
