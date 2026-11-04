"""Live read of the signed-in Spotify account's own playlist list.

The Spotify source panel used to list only playlists that had been imported
into state.db, so an account with 200 playlists showed the handful that had
been through ``python -m apps.spotify import``. This module lists every
playlist the account follows or owns (``GET /v1/me/playlists``, paged), so the
panel can show all of them and play any one in the Spotify side player.

It never starts an interactive sign-in by itself. The engine reads the token
the CLI or the in-app connect flow cached at ``TOKEN_CACHE_PATH`` and refreshes
it; with no usable token it reports why, as a ``NotConnected`` reason the UI
can render, instead of opening a browser from a request thread.

The in-app connect flow (:class:`ConnectFlow`) reuses the redirect URI the
developer app already registers (``http://127.0.0.1:8888/callback``): it binds
a one-shot loopback listener there, hands the caller the authorize URL to open,
and saves the token through spotipy's own cache handler.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Literal, Protocol, TypeGuard
from urllib.parse import parse_qs, urlparse

from apps.spotify.client import DEFAULT_REDIRECT_URI, DEFAULT_SCOPES, TOKEN_CACHE_PATH

__all__ = [
    "CALLBACK_RECEIVED_TEXT",
    "CLIENT_ID_PATH",
    "AccountPlaylist",
    "AccountPlaylists",
    "ClientIdUnreadable",
    "EnvClientIdInvalid",
    "ConnectFlow",
    "NotConnected",
    "NotConnectedReason",
    "PLAYER_ID_RE",
    "list_account_playlists",
    "normalize_client_id",
    "open_account_client",
    "playlist_open_url",
    "resolve_client_id",
    "save_client_id",
]

# What the sign-in tab says once Spotify's redirect arrives: never "connected",
# because the token exchange and the saves can still fail after it.
CALLBACK_RECEIVED_TEXT = "Spotify sent your sign-in to Open DJ. Open DJ shows whether it connected; you can close this tab."

# Spotify caps /me/playlists at 50 per page.
PAGE_SIZE = 50

# The PKCE client id is public (no secret), so it may live beside the token.
CLIENT_ID_PATH: Path = TOKEN_CACHE_PATH.with_name("spotify-client.json")
# Seconds a token exchange or refresh may take before it fails (spotipy waits forever by default).
TOKEN_REQUEST_TIMEOUT_S: float = 10

NotConnectedReason = Literal[
    "spotify_extra_missing",
    "no_client_id",
    "not_signed_in",
    "scope_missing",
    "token_refresh_failed",
]


class ClientIdUnreadable(Exception):
    """The saved client-id file exists but cannot be read or is not ours.

    Distinct from ``no_client_id``: treating a damaged file as absent would
    send the user through setup and overwrite the evidence.
    """

    def __init__(self, path: Path, problem: str) -> None:
        super().__init__(f"The saved Spotify client id at {path} cannot be read: {problem}")
        self.path: Path | None = path


class EnvClientIdInvalid(ClientIdUnreadable):
    """SPOTIFY_CLIENT_ID is set but is not a Client ID.

    Refused before PKCE starts, so Connect fails at once rather than opening a
    sign-in Spotify will reject and waiting out the deadline.
    """

    def __init__(self, value: str, problem: str) -> None:
        Exception.__init__(self, f"SPOTIFY_CLIENT_ID in the environment is not a Spotify Client ID ({value!r}): {problem}")
        self.path = None


class NotConnected(Exception):
    """No usable Spotify session; ``reason`` is a stable code the UI renders."""

    def __init__(self, reason: NotConnectedReason, message: str) -> None:
        super().__init__(message)
        self.reason: NotConnectedReason = reason
        self.message = message


# The id shape side-player-layout.ts and side-player-shell.ts accept.
PLAYER_ID_RE = re.compile(r"[A-Za-z0-9]+")


def playlist_open_url(spotify_id: str) -> str:
    """The side player's page for a playlist, built from its id only."""
    return f"https://open.spotify.com/playlist/{spotify_id}"


class AccountApi(Protocol):
    """The two spotipy calls this module needs; tests pass a fake."""

    def current_user(self) -> dict[str, Any]: ...

    def current_user_playlists(self, limit: int = 50, offset: int = 0) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AccountPlaylist:
    """One playlist row as the account sees it."""

    spotify_id: str
    name: str
    owner_id: str
    owner_name: str | None
    owned: bool
    collaborative: bool
    public: bool | None
    track_count: int | None
    image_url: str | None
    open_url: str
    snapshot_id: str | None


@dataclass(frozen=True)
class AccountPlaylists:
    user_id: str
    display_name: str | None
    playlists: list[AccountPlaylist]
    # Spotify's own count from the first page; lets the UI prove it got all.
    total_reported: int

    def to_json(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "total_reported": self.total_reported,
            "playlists": [asdict(p) for p in self.playlists],
        }


def _first_image_url(item: dict[str, Any], spotify_id: str) -> str | None:
    """The first cover image, or None when Spotify sent none.

    Absent or null images mean no cover; a non-list, or an entry that is not
    an object with a string url, is drift and fails.
    """
    images = item.get("images")
    if images is None:
        return None
    if not isinstance(images, list):
        raise _malformed_row(f"{spotify_id} images is {images!r}")
    for image in images:
        if not isinstance(image, dict) or not isinstance(image.get("url"), str):
            raise _malformed_row(f"{spotify_id} image entry is {image!r}")
    return images[0]["url"] if images else None


def _track_count(item: dict[str, Any], spotify_id: str) -> int | None:
    """The playlist's track total, or None when Spotify sent none.

    A present total that is not a non-negative integer fails: a bool passes
    isinstance(int) and would ship True as 1 track.
    """
    # Spotify renamed "tracks" to "items" on playlist objects in 2025; read both.
    for key in ("items", "tracks"):
        block = item.get(key)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise _malformed_row(f"{spotify_id} {key} is {block!r}")
        if "total" not in block:
            continue
        total = block["total"]
        if not _is_count(total):
            raise _malformed_row(f"{spotify_id} {key} total is {total!r}")
        return total
    return None


def _optional_str(mapping: dict[str, Any], key: str, where: str) -> str | None:
    """An absent or null field is None; any other non-string is drift."""
    value = mapping.get(key)
    if value is None or isinstance(value, str):
        return value
    raise _malformed_row(f"{where} {key} is {value!r}")


def _malformed_row(problem: str) -> RuntimeError:
    return RuntimeError(f"Spotify /me/playlists returned a malformed playlist: {problem}")


def _required_row_fields(item: dict[str, Any]) -> tuple[str, str, bool, bool | None]:
    """The id, name, collaborative and public fields, exactly as Spotify typed them.

    A missing or mistyped one fails instead of becoming an invented "" or a
    truthy-coerced flag.
    """
    spotify_id = item.get("id")
    # The side player's target guard admits only base-62 ids, so any other id
    # would give a play button whose page is refused.
    if not isinstance(spotify_id, str) or not PLAYER_ID_RE.fullmatch(spotify_id):
        raise _malformed_row(f"id is {spotify_id!r}")
    name = item.get("name")
    if not isinstance(name, str):
        raise _malformed_row(f"{spotify_id} name is {name!r}")
    collaborative = item.get("collaborative")
    if not isinstance(collaborative, bool):
        raise _malformed_row(f"{spotify_id} collaborative is {collaborative!r}")
    # Spotify sends an explicit null when the owner has not chosen; a missing
    # key is drift, not that null.
    if "public" not in item:
        raise _malformed_row(f"{spotify_id} has no public field")
    public = item["public"]
    if public is not None and not isinstance(public, bool):
        raise _malformed_row(f"{spotify_id} public is {public!r}")
    return spotify_id, name, collaborative, public


def _required_owner_id(item: dict[str, Any], spotify_id: str) -> str:
    """The owner's id, which decides ``owned``; a missing or empty one is drift.

    Read as no owner, an owned playlist would rank as followed.
    """
    owner = item.get("owner")
    if not isinstance(owner, dict):
        raise _malformed_row(f"{spotify_id} owner is {owner!r}")
    owner_id = owner.get("id")
    if not isinstance(owner_id, str) or owner_id == "":
        raise _malformed_row(f"{spotify_id} owner id is {owner_id!r}")
    return owner_id


def _parse_playlist(item: dict[str, Any], user_id: str) -> AccountPlaylist:
    spotify_id, name, collaborative, public = _required_row_fields(item)
    owner_id = _required_owner_id(item, spotify_id)
    owner = item["owner"]
    # Built from the id, never taken from external_urls: a URL Spotify sends
    # could name an album, an account page or another playlist.
    open_url = playlist_open_url(spotify_id)
    return AccountPlaylist(
        spotify_id=spotify_id,
        name=name,
        owner_id=owner_id,
        owner_name=_optional_str(owner, "display_name", f"{spotify_id} owner"),
        owned=owner_id == user_id,
        collaborative=collaborative,
        public=public,
        track_count=_track_count(item, spotify_id),
        image_url=_first_image_url(item, spotify_id),
        open_url=open_url,
        snapshot_id=_optional_str(item, "snapshot_id", spotify_id),
    )


def _add_playlist_rows(
    items: list[Any], user_id: str, seen: set[str], playlists: list[AccountPlaylist]
) -> None:
    for item in items:
        # Spotify returns null entries for playlists that were deleted; any
        # other non-object is drift and fails rather than silently shortening
        # the listing.
        if item is None:
            continue
        if not isinstance(item, dict):
            raise _malformed_row(f"entry {item!r} is not an object")
        row = _parse_playlist(item, user_id)
        # A playlist on two pages means the list moved under the offset walk;
        # the first copy is kept, and the next refresh sees the change whole.
        if row.spotify_id in seen:
            continue
        seen.add(row.spotify_id)
        playlists.append(row)


def _is_count(value: object) -> TypeGuard[int]:
    """A non-negative int; a bool is not a count, though isinstance(True, int) holds."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _checked_playlist_page(page: object, offset: int) -> tuple[list[Any], str | None, int]:
    """Return a /me/playlists page's items, next link and total, or raise.

    A missing or mistyped field must not read as an empty or final page, or a
    partial response becomes a successful but incomplete listing.
    """

    def malformed(problem: str) -> RuntimeError:
        return RuntimeError(f"Spotify /me/playlists returned a malformed page at offset {offset}: {problem}")

    if not isinstance(page, dict):
        raise malformed(f"expected an object, got {type(page).__name__}")
    items = page.get("items")
    if not isinstance(items, list):
        raise malformed(f"items is {type(items).__name__}, not a list")
    if "next" not in page:
        raise malformed("no next field")
    next_url = page["next"]
    if next_url is not None and (not isinstance(next_url, str) or next_url == ""):
        raise malformed(f"next is {next_url!r}, not a link or null")
    total = page.get("total")
    if not _is_count(total):
        raise malformed(f"total is {total!r}, not a count")
    # The page must say it is the slice that was asked for: a stale or
    # misrouted page whose next and total happen to agree would otherwise
    # pass every later check with the wrong rows.
    page_offset = page.get("offset")
    if not _is_count(page_offset) or page_offset != offset:
        raise malformed(f"offset is {page_offset!r}, not {offset}")
    return items, next_url, total


def _next_offset(next_url: str, page_end: int) -> int:
    """The offset Spotify's ``next`` link advertises, checked against where the page ended.

    The link is read rather than assumed, and it must
    start exactly where this page ended: a link past that skips rows and one
    before it repeats them, so either fails instead of returning a partial
    listing as complete.
    """
    values = parse_qs(urlparse(next_url).query).get("offset", [])
    if len(values) != 1 or not values[0].isdigit():
        raise RuntimeError(
            f"Spotify /me/playlists returned a malformed page ending at {page_end}: next {next_url!r} has no offset"
        )
    advertised = int(values[0])
    if advertised != page_end:
        raise RuntimeError(f"Spotify /me/playlists next link points at offset {advertised}, not {page_end} where the page ended")
    return advertised


def list_account_playlists(api: AccountApi) -> AccountPlaylists:
    """Every playlist on the account, in Spotify's own order, all pages.

    There is no page cap: the walk follows ``next`` until Spotify stops
    offering one. Only a chain that stops making progress is refused: a next
    page offered after an empty one, or a walk past the total Spotify itself
    reported. A follow or unfollow made during the walk can shift a row past
    the offsets; the next refresh lists it.
    """
    me = api.current_user()
    user_id = me.get("id")
    if not isinstance(user_id, str) or user_id == "":
        raise ValueError("Spotify /me returned no user id")
    playlists, total_reported = _walk_account_playlists(api, user_id)
    # Absent or null is Spotify's "no display name"; any other type is drift.
    display_name = me.get("display_name")
    if display_name is not None and not isinstance(display_name, str):
        raise RuntimeError(f"Spotify /me returned a display_name of {display_name!r}")
    return AccountPlaylists(
        user_id=user_id,
        display_name=display_name,
        playlists=playlists,
        total_reported=total_reported,
    )


def _walk_account_playlists(api: AccountApi, user_id: str) -> tuple[list[AccountPlaylist], int]:
    """One offset walk of /me/playlists: the rows and the first page's total."""
    playlists: list[AccountPlaylist] = []
    seen: set[str] = set()
    total_reported: int | None = None
    offset = 0
    while True:
        items, next_url, total = _checked_playlist_page(
            api.current_user_playlists(limit=PAGE_SIZE, offset=offset), offset
        )
        if total_reported is None:
            total_reported = total
        _add_playlist_rows(items, user_id, seen, playlists)
        page_end = offset + len(items)
        if next_url is None:
            if page_end != total_reported:
                raise RuntimeError(
                    f"Spotify /me/playlists stopped at {page_end} rows of its reported total of {total_reported}"
                )
            return playlists, total_reported
        if not items:
            raise RuntimeError(f"Spotify /me/playlists offered another page after an empty one at offset {offset}")
        offset = _next_offset(next_url, page_end)
        if offset > total_reported:
            raise RuntimeError(
                f"Spotify /me/playlists kept offering pages past its reported total of {total_reported}"
            )


def _env_client_id(source: Mapping[str, str]) -> str | None:
    """SPOTIFY_CLIENT_ID normalized, None when unset, or EnvClientIdInvalid."""
    value = source.get("SPOTIFY_CLIENT_ID", "").strip()
    if not value:
        return None
    try:
        return normalize_client_id(value)
    except ValueError as exc:
        raise EnvClientIdInvalid(value, str(exc)) from exc


def resolve_client_id(
    env: dict[str, str] | None = None, client_id_path: Path | None = None
) -> str | None:
    """SPOTIFY_CLIENT_ID from the env, else the id saved by the last sign-in."""
    from_env = _env_client_id(os.environ if env is None else env)
    if from_env is not None:
        return from_env
    path = CLIENT_ID_PATH if client_id_path is None else client_id_path
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ClientIdUnreadable(path, str(exc)) from exc
    except UnicodeDecodeError as exc:
        raise ClientIdUnreadable(path, f"not UTF-8 ({exc})") from exc
    try:
        saved = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClientIdUnreadable(path, f"not JSON ({exc})") from exc
    value = saved.get("client_id") if isinstance(saved, dict) else None
    if not isinstance(value, str) or value.strip() == "":
        raise ClientIdUnreadable(path, 'no "client_id" string in it')
    try:
        return normalize_client_id(value)
    except ValueError as exc:
        # The PUT route only ever saves a valid id, so anything else is damage.
        raise ClientIdUnreadable(path, f"the saved value is not a Spotify Client ID ({exc})") from exc


# A Spotify app's Client ID, as the developer dashboard shows it.
_CLIENT_ID_RE = re.compile(r"[0-9a-f]{32}")


def normalize_client_id(value: str) -> str:
    """The id lowercased and trimmed; ValueError unless it is 32 hex digits."""
    cleaned = value.strip().lower()
    if _CLIENT_ID_RE.fullmatch(cleaned) is None:
        raise ValueError("A Spotify Client ID is 32 letters and digits (0-9, a-f) from your app's dashboard page.")
    return cleaned


def save_client_id(client_id: str, client_id_path: Path | None = None) -> None:
    """Replace the saved id atomically: a failed write leaves the old file whole.

    Writing in place truncates first, so a full disk or a kill mid-write would
    leave a damaged file and SPOTIFY_CLIENT_ID_UNREADABLE.
    """
    path = CLIENT_ID_PATH if client_id_path is None else client_id_path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({"client_id": client_id}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _pkce(
    client_id: str,
    token_path: Path,
    state: str | None = None,
    token_url: str | None = None,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
) -> Any:
    try:
        from spotipy.cache_handler import CacheFileHandler
        from spotipy.oauth2 import SpotifyPKCE
    except ImportError as exc:  # the spotify extra is optional
        raise NotConnected(
            "spotify_extra_missing",
            "Spotify support is not installed in this engine (the 'spotify' extra).",
        ) from exc
    token_path.parent.mkdir(parents=True, exist_ok=True)
    auth = SpotifyPKCE(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=DEFAULT_SCOPES,
        cache_handler=CacheFileHandler(cache_path=str(token_path)),
        open_browser=False,
        state=state,
        # spotipy's default is no timeout: a token endpoint that accepts the
        # connection and stalls would hang Connect and every listing forever.
        requests_timeout=TOKEN_REQUEST_TIMEOUT_S,
    )
    if token_url is not None:
        # Where the code exchange and refresh POST go. Production leaves it
        # at spotipy's accounts.spotify.com; tests point it at a loopback
        # endpoint that answers with captured Spotify token responses.
        auth.OAUTH_TOKEN_URL = token_url
    return auth


def open_account_client(
    *,
    env: dict[str, str] | None = None,
    token_path: Path | None = None,
    client_id_path: Path | None = None,
    token_url: str | None = None,
    api_base: str | None = None,
) -> AccountApi:
    """A spotipy client on the cached token, refreshed; never prompts."""
    client_id = resolve_client_id(env, client_id_path)
    if client_id is None:
        raise NotConnected(
            "no_client_id",
            "No Spotify client id: paste your Spotify app's Client ID in the Spotify "
            "panel, set SPOTIFY_CLIENT_ID, or sign in once with `python -m apps.spotify login`.",
        )
    path = TOKEN_CACHE_PATH if token_path is None else token_path
    auth = _pkce(client_id, path, token_url=token_url)
    cached = auth.cache_handler.get_cached_token()
    if cached is None:
        raise NotConnected("not_signed_in", "Not signed in to Spotify.")
    if not auth._is_scope_subset(DEFAULT_SCOPES, cached.get("scope", "")):
        raise NotConnected(
            "scope_missing",
            "The saved Spotify sign-in cannot read private playlists; sign in again.",
        )
    from spotipy.exceptions import SpotifyOauthError

    try:
        token = auth.validate_token(cached)
    except SpotifyOauthError as exc:
        # Only Spotify refusing the refresh token is a dead sign-in. Anything
        # else (a 5xx, an unexpected body, a network failure, a defect here)
        # propagates, so the route reports a failure instead of asking the
        # user to sign in again over it.
        if exc.error != "invalid_grant":
            raise
        raise NotConnected("token_refresh_failed", f"Spotify sign-in expired: {exc}") from exc
    if token is None:
        raise NotConnected("not_signed_in", "Not signed in to Spotify.")
    if token.get("access_token") != cached.get("access_token"):
        # A refresh happened. spotipy logs a failed cache write and carries
        # on, so read it back: a client on credentials that were never saved
        # refreshes again on every call, or dies once Spotify rotates the
        # refresh token.
        saved = auth.cache_handler.get_cached_token()
        if not isinstance(saved, dict) or saved.get("access_token") != token.get("access_token"):
            raise RuntimeError(f"the refreshed Spotify sign-in could not be saved to {path}")
    import spotipy

    client = spotipy.Spotify(auth_manager=auth, requests_timeout=10, retries=2)
    if api_base is not None:
        # Production talks to api.spotify.com; tests point the same client at
        # a loopback server answering with captured Web API responses.
        client.prefix = api_base
    return client


EPHEMERAL_REDIRECT_ENV = "OPENDJ_SPOTIFY_EPHEMERAL_REDIRECT"


def _ephemeral_redirect(env: Mapping[str, str]) -> bool:
    """True when a test harness asked for the sign-in listener on any free port.

    Spotify accepts only the redirect URI the developer app registered
    (port 8888), so a real sign-in never sets this. Suites that run on one
    shared host at once set it so their Connect listeners cannot collide on
    8888. Any value but unset, empty or "1" fails.
    """
    value = env.get(EPHEMERAL_REDIRECT_ENV, "")
    if value not in ("", "1"):
        raise RuntimeError(f"{EPHEMERAL_REDIRECT_ENV} is {value!r}; set it to 1 or leave it unset")
    return value == "1"


def _persist_signed_in_client_id(client_id: str, client_id_path: Path | None) -> str | None:
    """Save the id a sign-in just used; the failure text, or None when done.

    The id was normalized when it was resolved; the check here keeps anything
    else from being written where it would read back as a damaged file.
    """
    try:
        cleaned = normalize_client_id(client_id)
    except ValueError:
        return None
    try:
        save_client_id(cleaned, client_id_path)
    except OSError as exc:
        return f"signed in, but the client id could not be saved: {exc}"
    return None


ConnectPhase = Literal["idle", "waiting", "connected", "failed"]

# How long the loopback listener waits for the browser to come back.
CONNECT_TIMEOUT_S = 300
# How long one accepted callback socket may sit without sending its request.
# server.timeout bounds only the wait for a connection: without this, a local
# client that connects and never finishes would block the listener past both
# the flow deadline and a replacing Connect.
CALLBACK_READ_TIMEOUT_S = 2.0


class ConnectFlow:
    """One in-app sign-in at a time: authorize URL out, loopback code in.

    ``start`` binds 127.0.0.1:<redirect port> first, so a busy port (another
    sign-in, or the CLI's own listener) fails loudly before any URL is handed
    out. The listener thread exchanges the code through spotipy, which writes
    the token cache, then closes.
    """

    def __init__(
        self,
        *,
        token_path: Path | None = None,
        client_id_path: Path | None = None,
        server_factory: Callable[[tuple[str, int], type[BaseHTTPRequestHandler]], HTTPServer] = HTTPServer,
        timeout_s: float = CONNECT_TIMEOUT_S,
        token_url: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._token_path = TOKEN_CACHE_PATH if token_path is None else token_path
        self._client_id_path = client_id_path
        # Where SPOTIFY_CLIENT_ID is read; None is the process environment.
        self._env = env
        self._server_factory = server_factory
        self._timeout_s = timeout_s
        self._token_url = token_url
        self._lock = threading.Lock()
        # Held for a whole start: cancel the old sign-in, bind, publish and
        # start the listener. Without it a second start could find no
        # thread to stop and leave two listeners.
        self._start_lock = threading.Lock()
        self._phase: ConnectPhase = "idle"
        self._detail: str | None = None
        self._server: HTTPServer | None = None
        self._cancel: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"phase": self._phase, "detail": self._detail}

    def _finish(self, phase: ConnectPhase, detail: str | None) -> None:
        with self._lock:
            self._phase = phase
            self._detail = detail

    def start(self, env: dict[str, str] | None = None) -> str:
        """Return the authorize URL; raises NotConnected, ClientIdUnreadable or OSError (port busy)."""
        with self._start_lock:
            return self._start(env)

    def _start(self, env: dict[str, str] | None) -> str:
        source = self._env if env is None else env
        ephemeral = _ephemeral_redirect(os.environ if source is None else source)
        client_id = resolve_client_id(source, self._client_id_path)
        if client_id is None:
            raise NotConnected(
                "no_client_id",
                "No Spotify client id: paste your Spotify app's Client ID in the Spotify panel first.",
            )
        self._cancel_waiting()
        state = secrets.token_urlsafe(16)
        redirect = urlparse(DEFAULT_REDIRECT_URI)
        flow = self
        result: dict[str, str] = {}

        class _Handler(BaseHTTPRequestHandler):
            timeout = CALLBACK_READ_TIMEOUT_S

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                parsed = urlparse(self.path)
                if parsed.path != redirect.path:
                    self.send_response(404)
                    self.end_headers()
                    return
                query = parse_qs(parsed.query)
                if (query.get("state") or [""])[0] != state:
                    # A stale tab from a replaced Connect (or a forged hit) on
                    # the same redirect URL: answer it, keep waiting for ours.
                    body = b"This Spotify sign-in was replaced by a newer one. Close this tab."
                else:
                    result["state"] = state
                    result["code"] = (query.get("code") or [""])[0]
                    result["error"] = (query.get("error") or [""])[0]
                    ok = result["error"] == "" and result["code"] != ""
                    # The exchange and the saves run after this answer, so the
                    # tab only says the sign-in arrived; the panel reports
                    # whether it connected.
                    body = (
                        CALLBACK_RECEIVED_TEXT
                        if ok
                        else "Spotify sign-in did not complete. Close this tab and try again."
                    ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        host = redirect.hostname or "127.0.0.1"
        server = self._server_factory((host, 0 if ephemeral else redirect.port or 80), _Handler)
        try:
            # The listener is bound before the URL is built, so an ephemeral
            # port is known; the exchange sends the same redirect URI.
            redirect_uri = (
                f"http://{host}:{server.server_address[1]}{redirect.path}" if ephemeral else DEFAULT_REDIRECT_URI
            )
            auth = _pkce(client_id, self._token_path, state=state, token_url=self._token_url, redirect_uri=redirect_uri)
            url = auth.get_authorize_url()
        except BaseException:
            server.server_close()
            raise
        server.timeout = 1.0
        cancel = threading.Event()
        with self._lock:
            self._phase = "waiting"
            self._detail = None
            self._server = server
            self._cancel = cancel

        def _run() -> None:
            deadline = time.monotonic() + flow._timeout_s
            try:
                while "code" not in result and time.monotonic() < deadline and not cancel.is_set():
                    server.handle_request()
            finally:
                server.server_close()
            if cancel.is_set():
                return  # a newer Connect replaced this one and owns the phase now
            if "code" not in result:
                flow._finish("failed", "timed out waiting for the browser")
                return
            if result.get("error"):
                flow._finish("failed", f"Spotify said: {result['error']}")
                return
            # spotipy reads an empty code as "go and get one", which prompts on
            # stdin instead of failing.
            if not result["code"]:
                flow._finish("failed", "Spotify sent no sign-in code")
                return
            try:
                access_token = auth.get_access_token(code=result["code"], check_cache=False)
            except Exception as exc:
                flow._finish("failed", f"token exchange failed: {exc}")
                return
            # spotipy logs a failed cache write and carries on, so read the
            # sign-in back: success means the next listing can use it.
            saved = auth.cache_handler.get_cached_token()
            if not isinstance(saved, dict) or saved.get("access_token") != access_token:
                flow._finish("failed", f"the Spotify sign-in could not be saved to {flow._token_path}")
                return
            problem = _persist_signed_in_client_id(client_id, flow._client_id_path)
            flow._finish("connected" if problem is None else "failed", problem)

        thread = threading.Thread(target=_run, name="spotify-connect", daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()
        return url

    def _cancel_waiting(self) -> None:
        """Replace a sign-in still waiting for the browser (say its tab was closed).

        Its listener stops within one ``handle_request`` timeout and frees the
        redirect port before the new one binds it.
        """
        with self._lock:
            if self._phase != "waiting":
                return
            cancel, thread = self._cancel, self._thread
        if cancel is not None:
            cancel.set()
        if thread is not None:
            thread.join(timeout=5.0)
            if thread.is_alive():
                raise RuntimeError("the previous Spotify sign-in did not stop; try again")
