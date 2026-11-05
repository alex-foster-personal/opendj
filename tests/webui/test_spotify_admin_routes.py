"""HTTP contract for the two /admin Spotify panes.

if /status reports connected without a live profile call -> broken
if a Spotify failure renders as an empty play list -> broken
if the production app does not expose both paths -> broken
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import schema as state_schema
from apps.spotify.client import ReauthorizationRequired
from apps.spotify.client import SpotifyClient as RealSpotifyClient
from apps.spotify.purchase_links import suggested_sources
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.routes import spotify as spotify_routes

_RECENTLY_PLAYED_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "spotify" / "recently-played.json"
_RECENTLY_PLAYED_SHA256 = "57afc93c54eef45c64eb73747dc958415cec2727d1e5299b09170c6d3ecd3150"


class _HistorySpotipy:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.limits: list[int] = []

    def current_user_recently_played(self, limit: int = 50) -> dict:
        self.limits.append(limit)
        return self.payload


def _seed_library(db_path: Path) -> None:
    created = "2026-09-22T08:00:00Z"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, "
        "artists_json, album, isrc, duration_ms, file_path, "
        "content_hash, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "local-owned-isrc",
            "isrc",
            "Other Title",
            json.dumps(["Other Artist"]),
            "Local Album",
            "USFIX2600001",
            120_000,
            None,
            None,
            created,
            created,
        ),
    )
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, "
        "artists_json, album, isrc, duration_ms, file_path, "
        "content_hash, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "local-review-tags",
            "inferred",
            "Review Cut",
            json.dumps(["Fixture Artist"]),
            None,
            None,
            210_000,
            None,
            None,
            created,
            created,
        ),
    )
    conn.close()


def _make_client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    state_schema.apply_migrations(conn)
    conn.close()

    app = FastAPI()
    app.state.backend = InMemoryBackend()
    app.state.state_db_path = db_path
    app.include_router(spotify_routes.router, prefix="/api/v1")
    return TestClient(app)


@pytest.mark.requirement("CAT-01")
def test_production_app_registers_both_admin_paths() -> None:
    """[if] the production app is built [then] its OpenAPI lists both Spotify admin paths, [else stop]"""
    paths = create_app(mount_frontend=False).openapi()["paths"]

    assert "/api/v1/spotify/status" in paths
    assert "/api/v1/spotify/recently-played" in paths


@pytest.mark.requirement("CAT-01")
def test_status_without_client_id_is_not_connected_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] SPOTIFY_CLIENT_ID is unset [then] status reads not connected and names the variable, [else stop]"""
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    body = _make_client(tmp_path).get("/api/v1/spotify/status").json()

    assert body["connected"] is False
    assert body["client_id_present"] is False
    assert "SPOTIFY_CLIENT_ID" in body["error"]


@pytest.mark.requirement("CAT-01")
def test_status_with_client_id_but_no_token_is_not_connected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A client id alone is not a connection, and the pane must not imply it is.

    [if] a client id is set but no token is cached [then] status reads not connected and says no cached token, [else stop]
    """
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "deadbeef")
    monkeypatch.setattr(spotify_routes, "TOKEN_CACHE_PATH", tmp_path / "absent-token.json")
    body = _make_client(tmp_path).get("/api/v1/spotify/status").json()

    assert body["connected"] is False
    assert body["client_id_present"] is True
    assert body["token_cache_present"] is False
    assert "No cached token" in body["error"]


@pytest.mark.requirement("CAT-01")
def test_status_reports_connected_only_after_a_live_profile_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the live profile call succeeds [then] status reads connected with the display name and product, [else stop]"""
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "deadbeef")
    monkeypatch.setattr(spotify_routes, "TOKEN_CACHE_PATH", token)

    class _Client:
        @staticmethod
        def from_env(**_kwargs: object) -> _Client:
            return _Client()

        def current_user(self) -> dict:
            return {"display_name": "the maintainer", "id": "alexf", "product": "premium"}

    monkeypatch.setattr(spotify_routes, "SpotifyClient", _Client)
    body = _make_client(tmp_path).get("/api/v1/spotify/status").json()

    assert body["connected"] is True
    assert body["display_name"] == "the maintainer"
    assert body["product"] == "premium"
    assert body["error"] is None


@pytest.mark.requirement("CAT-01")
def test_status_surfaces_a_failing_profile_call_instead_of_claiming_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] building the client fails [then] status reads not connected and carries the real error, [else stop]"""
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "deadbeef")
    monkeypatch.setattr(spotify_routes, "TOKEN_CACHE_PATH", token)

    class _Client:
        @staticmethod
        def from_env(**_kwargs: object) -> _Client:
            raise RuntimeError("token revoked")

    monkeypatch.setattr(spotify_routes, "SpotifyClient", _Client)
    body = _make_client(tmp_path).get("/api/v1/spotify/status").json()

    assert body["connected"] is False
    assert "token revoked" in body["error"]


@pytest.mark.requirement("CAT-3807")
def test_recently_played_scores_plays_against_the_local_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] recently-played is asked for N plays [then] it fetches N and scores each owned/review/unmatched, [else stop]"""
    raw = _RECENTLY_PLAYED_FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _RECENTLY_PLAYED_SHA256
    payload = json.loads(raw)
    spotipy = _HistorySpotipy(payload)

    class _RouteClient:
        @staticmethod
        def from_env(**_kwargs: object) -> RealSpotifyClient:
            return RealSpotifyClient(spotipy)

    monkeypatch.setattr(spotify_routes, "SpotifyClient", _RouteClient)
    client = _make_client(tmp_path)
    _seed_library(tmp_path / "state.db")
    response = client.get("/api/v1/spotify/recently-played?limit=10")

    assert response.status_code == 200
    assert spotipy.limits == [10]
    rows = response.json()
    assert len(rows) == 3
    assert [row["title"] for row in rows] == [
        "Review Cut",
        "Unknown Cut",
        "Owned Cut",
    ]

    review = rows[0]
    assert review["ownership"] == "review"
    assert review["matched_stable_id"] == "local-review-tags"
    assert review["confidence"] == 0.55
    assert review["suggested_sources"] == suggested_sources(review["artist"], review["title"])

    unmatched = rows[1]
    assert unmatched["ownership"] == "unmatched"
    assert unmatched["matched_stable_id"] is None
    assert unmatched["confidence"] == 0.0
    assert unmatched["suggested_sources"] == suggested_sources(unmatched["artist"], unmatched["title"])

    owned = rows[2]
    assert owned["ownership"] == "owned"
    assert owned["matched_stable_id"] == "local-owned-isrc"
    assert owned["confidence"] == 0.35
    assert owned["isrc"] == "USFIX2600001"
    assert owned["suggested_sources"] == suggested_sources(owned["artist"], owned["title"])


@pytest.mark.requirement("CAT-3807")
def test_recently_played_missing_spotipy_is_503_not_a_bare_500(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] from_env raises ModuleNotFoundError [then] recently-played answers 503 SPOTIFY_EXTRA_MISSING, [else stop]"""

    class _Client:
        @staticmethod
        def from_env(**_kwargs: object) -> _Client:
            raise ModuleNotFoundError("No module named 'spotipy'", name="spotipy")

    monkeypatch.setattr(spotify_routes, "SpotifyClient", _Client)
    response = _make_client(tmp_path).get("/api/v1/spotify/recently-played")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "SPOTIFY_EXTRA_MISSING"
    assert "spotify" in detail["message"].lower()


@pytest.mark.requirement("CAT-01")
def test_recently_played_upstream_failure_is_a_502_not_an_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty list must only ever mean "you played nothing".

    [if] the Spotify history call fails [then] the route answers 502 SPOTIFY_HISTORY_UNAVAILABLE, never an empty list, [else stop]
    """

    def _boom(client: object, state_path: Path, *, limit: int) -> list:
        raise RuntimeError("spotify 429")

    monkeypatch.setattr(
        spotify_routes,
        "SpotifyClient",
        type("C", (), {"from_env": staticmethod(lambda **_kwargs: object())}),
    )
    monkeypatch.setattr(spotify_routes, "fetch_recent_plays", _boom)

    response = _make_client(tmp_path).get("/api/v1/spotify/recently-played")

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "SPOTIFY_HISTORY_UNAVAILABLE"


@pytest.mark.requirement("CAT-01")
def test_recently_played_rejects_a_limit_spotify_cannot_serve(tmp_path: Path) -> None:
    """[if] the limit is above 50 or below 1 [then] the route answers 422, [else stop]"""
    assert _make_client(tmp_path).get("/api/v1/spotify/recently-played?limit=51").status_code == 422
    assert _make_client(tmp_path).get("/api/v1/spotify/recently-played?limit=0").status_code == 422


@pytest.mark.requirement("CAT-01")
def test_status_never_builds_an_interactive_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A daemon opening a browser would hang the request on a headless host.

    [if] status builds a Spotify client [then] it passes interactive=False, [else stop]
    """
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "deadbeef")
    monkeypatch.setattr(spotify_routes, "TOKEN_CACHE_PATH", token)
    seen: dict[str, object] = {}

    class _Client:
        @staticmethod
        def from_env(**kwargs: object) -> _Client:
            seen.update(kwargs)
            return _Client()

        def current_user(self) -> dict:
            return {"display_name": "the maintainer", "id": "alexf", "product": "premium"}

    monkeypatch.setattr(spotify_routes, "SpotifyClient", _Client)
    _make_client(tmp_path).get("/api/v1/spotify/status")

    assert seen["interactive"] is False


@pytest.mark.requirement("CAT-01")
def test_status_reports_needs_authorization_rather_than_a_bare_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scope change is a sign-in prompt, not an error the operator cannot act on.

    [if] the cached token lacks the scopes [then] status reads needs_authorization with the reason, [else stop]
    """
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "deadbeef")
    monkeypatch.setattr(spotify_routes, "TOKEN_CACHE_PATH", token)

    class _Client:
        @staticmethod
        def from_env(**_kwargs: object) -> _Client:
            raise ReauthorizationRequired("No usable cached Spotify token for scopes 'x'")

    monkeypatch.setattr(spotify_routes, "SpotifyClient", _Client)
    body = _make_client(tmp_path).get("/api/v1/spotify/status").json()

    assert body["connected"] is False
    assert body["needs_authorization"] is True
    assert "cached Spotify token" in body["error"]


@pytest.mark.requirement("CAT-01")
def test_recently_played_reauth_is_a_401_distinct_from_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the client needs reauthorization [then] recently-played answers 401 SPOTIFY_REAUTH_REQUIRED, [else stop]"""

    class _Client:
        @staticmethod
        def from_env(**_kwargs: object) -> _Client:
            raise ReauthorizationRequired("sign in once")

    monkeypatch.setattr(spotify_routes, "SpotifyClient", _Client)
    response = _make_client(tmp_path).get("/api/v1/spotify/recently-played")

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "SPOTIFY_REAUTH_REQUIRED"


@pytest.mark.requirement("CAT-01")
def test_recently_played_never_builds_an_interactive_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The history route shares the browser-hang failure mode with /status.

    [if] recently-played builds a Spotify client [then] it passes interactive=False, [else stop]
    """
    seen: dict[str, object] = {}

    class _Client:
        @staticmethod
        def from_env(**kwargs: object) -> _Client:
            seen.update(kwargs)
            return _Client()

    monkeypatch.setattr(spotify_routes, "SpotifyClient", _Client)
    monkeypatch.setattr(spotify_routes, "fetch_recent_plays", lambda *_a, **_k: [])
    _make_client(tmp_path).get("/api/v1/spotify/recently-played")

    assert seen["interactive"] is False
