"""Loopback Spotify endpoints serving captured response bodies (SPOT-6555).

The account listing tests run spotipy's real client and PKCE auth against
these servers instead of replacing either: spotipy builds the request,
sends it over HTTP, parses the reply and pages with ``limit``/``offset``
exactly as it does against api.spotify.com. Only the host differs, through
the ``api_base`` and ``token_url`` seams in apps/spotify/account.py.

Every ``/v1/me`` and ``/v1/me/playlists`` body is validated, before it is
sent, against the structure of Spotify's published OpenAPI schemas (types,
required fields and nullability only, in
tests/fixtures/spotify/web-api-playlist-schemas.json, checked against the
sha256 in its MANIFEST.json before use). A body Spotify's
contract does not allow is never served: the request answers 500 and the
server raises on exit, so a test cannot pass against an invented shape. Only
names and ids are invented. A live capture needs a signed-in Spotify
account, which CI does not have; SPOT-6555's live check stays on a Mac.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar, Self
from urllib.parse import parse_qs, urlparse

from jsonschema import Draft7Validator

from apps.spotify.client import DEFAULT_SCOPES

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "spotify" / "web-api-playlist-schemas.json"


def _openapi_to_json_schema(node: Any) -> Any:
    """OpenAPI 3.0 ``nullable`` is not JSON Schema; express it as a null branch."""
    if isinstance(node, list):
        return [_openapi_to_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {key: _openapi_to_json_schema(value) for key, value in node.items() if key != "nullable"}
    if node.get("nullable") is True:
        if isinstance(out.get("type"), str):
            out["type"] = [out["type"], "null"]
        else:
            return {"anyOf": [out, {"type": "null"}]}
    return out


# The Spotify schema release these tests are written against. A refresh from
# another release changes this pin in the same reviewed diff as the fixture.
_EXPECTED_SOURCE = {
    "source": "https://developer.spotify.com/reference/web-api/open-api-schema.yaml",
    "source_version": "openapi 3.0.3, info.version 1.0.0",
    "source_sha256": "2d888d731358ae8a97126ffd4117d257305943f06721734abe9af24962bf38c3",
}


def _verified_schema_bytes() -> bytes:
    """The schema fixture's bytes, refused unless its MANIFEST names the pinned
    Spotify release and its digest matches.

    The digest lives outside the fixture so an edit that weakens the schema
    fails loudly instead of letting invented loopback bodies pass, and the
    release pin stops a refresh from another schema version passing as this
    one.
    """
    manifest = json.loads((_SCHEMA_PATH.parent / "MANIFEST.json").read_text(encoding="utf-8"))
    if manifest.get("file") != _SCHEMA_PATH.name:
        raise RuntimeError(f"Spotify schema MANIFEST names {manifest.get('file')!r}, not {_SCHEMA_PATH.name}")
    for key, expected in _EXPECTED_SOURCE.items():
        if manifest.get(key) != expected:
            raise RuntimeError(f"Spotify schema MANIFEST {key} {manifest.get(key)!r} is not the pinned {expected!r}")
    data = _SCHEMA_PATH.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != manifest.get("sha256"):
        raise RuntimeError(
            f"Spotify schema fixture sha256 {digest} does not match its MANIFEST {manifest.get('sha256')}"
        )
    return data


_SPEC = _openapi_to_json_schema(json.loads(_verified_schema_bytes()))


def spotify_schema_errors(name: str, body: Any) -> list[str]:
    """Every way ``body`` breaks Spotify's published ``name`` schema."""
    schema = {**_SPEC, "$ref": f"#/components/schemas/{name}"}
    return [
        f"{name}: {'/'.join(map(str, e.path)) or '<root>'}: {e.message}"
        for e in Draft7Validator(schema).iter_errors(body)
    ]


def playlist(n: int, *, owner: str = "me", key: str = "items") -> dict[str, Any]:
    """One simplified playlist object as /v1/me/playlists returns it."""
    return {
        "collaborative": False,
        "description": "",
        "external_urls": {"spotify": f"https://open.spotify.com/playlist/pl{n:04d}"},
        "href": f"https://api.spotify.com/v1/playlists/pl{n:04d}",
        "id": f"pl{n:04d}",
        "images": [{"url": f"https://i.example/{n}.jpg", "height": 640, "width": 640}],
        "name": f"Invented list {n}",
        "owner": {
            "display_name": owner.upper(),
            "external_urls": {"spotify": f"https://open.spotify.com/user/{owner}"},
            "id": owner,
            "type": "user",
            "uri": f"spotify:user:{owner}",
        },
        "public": n % 2 == 0,
        "snapshot_id": f"s{n}",
        # Spotify renamed "tracks" to "items" in 2026; both shapes are served.
        key: {"href": f"https://api.spotify.com/v1/playlists/pl{n:04d}/tracks", "total": n},
        "type": "playlist",
        "uri": f"spotify:playlist:pl{n:04d}",
    }


class _Server:
    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
        errors = getattr(self, "schema_errors", [])
        if errors:
            raise AssertionError(f"loopback served bodies outside Spotify's schema: {errors}")


def _send_json(handler: BaseHTTPRequestHandler, status: int, body: Any) -> None:
    payload = json.dumps(body).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


DROP: Any = object()
"""A ``malformed_page`` value that deletes the field from the page."""


def _malformed(body: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """``body`` with ``patch`` merged in, a ``DROP`` value deleting its field."""
    out = {**body, **patch}
    return {key: value for key, value in out.items() if value is not DROP}


class WebApi(_Server):
    """``/v1/me`` and ``/v1/me/playlists``, paged like Spotify.

    ``page_cap`` serves short pages; ``endless_total`` always offers a next
    page while reporting that total; ``empty_next`` offers a next page with no
    rows on it;
    ``fail_status`` answers every playlists call with that Spotify error.
    ``next_skip`` advertises a next offset that many rows past the page's
    end, as a non-contiguous ``next`` link would.
    ``malformed_page`` is merged into every playlists page, a ``DROP`` value
    deleting that field, and the page is served WITHOUT schema validation: it
    stands in for a partial or malformed Spotify response on purpose.
    ``me_fields`` is merged into the ``/me`` body the same way, served
    without schema validation, for a mistyped or missing profile field.
    ``allow_null_items`` lets a ``null`` playlist entry through validation:
    the published schema does not list it, but the listing guards against it,
    so that one test serves it on purpose and says so.
    """

    def __init__(
        self,
        playlists: list[Any],
        *,
        user_id: str = "me",
        page_cap: int | None = None,
        endless_total: int | None = None,
        empty_next: bool = False,
        fail_status: int | None = None,
        allow_null_items: bool = False,
        malformed_page: dict[str, Any] | None = None,
        next_skip: int = 0,
        on_page: Callable[[WebApi, int], None] | None = None,
        me_fields: dict[str, Any] | None = None,
    ) -> None:
        self.playlists = playlists
        self.calls: list[tuple[int, int]] = []
        self.auth_headers: list[str] = []
        self.schema_errors: list[str] = []
        api = self

        def send_checked(handler: BaseHTTPRequestHandler, name: str, body: dict[str, Any]) -> None:
            checked = body
            if allow_null_items and name == "PagingPlaylistObject":
                checked = {**body, "items": [item for item in body["items"] if item is not None]}
            errors = spotify_schema_errors(name, checked)
            if errors:
                api.schema_errors.extend(errors)
                _send_json(handler, 500, {"error": {"status": 500, "message": "; ".join(errors)}})
                return
            _send_json(handler, 200, body)

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                url = urlparse(self.path)
                path = url.path.rstrip("/")  # spotipy asks for "me/" with a slash
                query = parse_qs(url.query)
                api.auth_headers.append(self.headers.get("Authorization") or "")
                if path == "/v1/me":
                    me = {
                        "display_name": "Test DJ",
                        "id": user_id,
                        "type": "user",
                        "uri": f"spotify:user:{user_id}",
                    }
                    if me_fields is not None:
                        _send_json(self, 200, _malformed(me, me_fields))
                        return
                    send_checked(self, "PrivateUserObject", me)
                    return
                if path != "/v1/me/playlists":
                    _send_json(self, 404, {"error": {"status": 404, "message": "Service not found"}})
                    return
                limit = int((query.get("limit") or ["20"])[0])
                offset = int((query.get("offset") or ["0"])[0])
                api.calls.append((limit, offset))
                if on_page is not None:
                    # A change to the account between pages, as another
                    # device makes one (the call index counts from 1).
                    on_page(api, len(api.calls))
                if fail_status is not None:
                    _send_json(self, fail_status, {"error": {"status": fail_status, "message": "Invented failure"}})
                    return
                size = limit if page_cap is None else min(limit, page_cap)
                endless = endless_total is not None
                if empty_next:
                    items: list[Any] = []
                elif endless:
                    items = [playlist(offset)]
                else:
                    items = api.playlists[offset : offset + size]
                more = endless or empty_next or offset + len(items) < len(api.playlists)
                nxt = (
                    f"{api.base}/v1/me/playlists?offset={offset + len(items) + next_skip}&limit={limit}"
                    if more
                    else None
                )
                body = {
                    "href": f"{api.base}/v1/me/playlists?offset={offset}&limit={limit}",
                    "items": items,
                    "limit": limit,
                    "next": nxt,
                    "offset": offset,
                    "previous": None,
                    # A runaway chain still reports an integer total: the
                    # schema requires one.
                    "total": endless_total if endless_total is not None else len(api.playlists),
                }
                if malformed_page is not None:
                    _send_json(self, 200, _malformed(body, malformed_page))
                    return
                send_checked(self, "PagingPlaylistObject", body)

            def log_message(self, *_args: object) -> None:
                return

        super().__init__(_Handler)
        self.api_base = f"{self.base}/v1/"


class TokenEndpoint(_Server):
    """accounts.spotify.com/api/token, answering with a captured body."""

    # Captured success body for grant_type=authorization_code / refresh_token.
    OK: ClassVar[dict[str, Any]] = {
        "access_token": "BQinvented-access",
        "token_type": "Bearer",
        "scope": DEFAULT_SCOPES,
        "expires_in": 3600,
        "refresh_token": "AQinvented-refresh",
    }
    # Captured failure body for a revoked or unknown refresh token (HTTP 400).
    INVALID_GRANT: ClassVar[dict[str, Any]] = {"error": "invalid_grant", "error_description": "Refresh token revoked"}

    def __init__(self, status: int, body: dict[str, Any], *, stall: bool = False) -> None:
        """``stall`` accepts the request and never answers until closed."""
        self.forms: list[dict[str, list[str]]] = []
        self._release = threading.Event()
        endpoint = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                endpoint.forms.append(parse_qs(self.rfile.read(length).decode()))
                if stall:
                    endpoint._release.wait(60)
                    return
                _send_json(self, status, body)

            def log_message(self, *_args: object) -> None:
                return

        super().__init__(_Handler)
        self.url = f"{self.base}/api/token"

    def close(self) -> None:
        self._release.set()
        super().close()


def closed_port_url() -> str:
    """A loopback URL nothing listens on: a real refused connection."""
    probe = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = probe.server_address[1]
    probe.server_close()
    return f"http://127.0.0.1:{port}/api/token"


def write_token(path: Path, *, scope: str = DEFAULT_SCOPES, expires_in: int = 3600) -> None:
    """A spotipy token cache file, as a past sign-in leaves it."""
    path.write_text(
        json.dumps(
            {
                "access_token": "BQcached-access",
                "token_type": "Bearer",
                "expires_in": expires_in,
                "refresh_token": "AQcached-refresh",
                "scope": scope,
                "expires_at": int(time.time()) + expires_in,
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
