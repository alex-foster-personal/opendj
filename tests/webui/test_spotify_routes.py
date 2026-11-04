"""Read-only HTTP contract for imported Spotify pending tracks."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.shared.state import schema as state_schema
from apps.spotify.state_writer import ensure_aux_tables
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Playlist
from apps.webui.server.routes.spotify import AccountPlaylistsOut, router


def _make_client(
    tmp_path: Path,
    *,
    playlist: Playlist | None = None,
    with_pending_table: bool = True,
) -> tuple[TestClient, sqlite3.Connection]:
    """Mount only the Spotify router around a real, test-local state DB."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    state_schema.apply_migrations(conn)
    if with_pending_table:
        ensure_aux_tables(conn)

    backend = InMemoryBackend()
    if playlist is not None:
        backend.seed_playlist(playlist)

    app = FastAPI()
    app.state.backend = backend
    app.state.state_db_path = db_path
    app.include_router(router, prefix="/api/v1")
    return TestClient(app), conn


def _spotify_playlist() -> Playlist:
    return Playlist(
        playlist_id="spotify:pl123",
        name="Imported Spotify Playlist",
        vendor="spotify",
        vendor_pl_id="pl123",
    )


@pytest.mark.requirement("CAT-01")
def test_production_app_registers_spotify_pending_track_contract() -> None:
    """The route must be reachable from the production app, not only a test mount."""
    paths = create_app(mount_frontend=False).openapi()["paths"]

    assert "/api/v1/spotify/playlists/{playlist_id}/pending-tracks" in paths


def _insert_pending(
    conn: sqlite3.Connection,
    *,
    position: int,
    status: str = "pending",
    sources: object | None = None,
) -> None:
    conn.execute(
        "INSERT INTO playlists "
        "(playlist_id, name, vendor, vendor_pl_id, created_at, updated_at) "
        "VALUES ('spotify:pl123', 'Imported Spotify Playlist', 'spotify', "
        "'pl123', '2026-07-22T00:00:00Z', '2026-07-22T00:00:00Z') "
        "ON CONFLICT(playlist_id) DO NOTHING"
    )
    conn.execute(
        "INSERT INTO pending_tracks "
        "(playlist_id, position, spotify_uri, isrc, title, artist, album, "
        "duration_ms, suggested_sources_json, status, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "spotify:pl123", position, f"spotify:track:{position}",
            "USABC2500001", f"Track {position}", "Artist", "Album", 123000,
            json.dumps(sources if sources is not None else {
                "beatport": "https://www.beatport.com/search?q=track",
                "bandcamp": "https://bandcamp.com/search?q=track",
                "qobuz": "https://www.qobuz.com/us-en/search?q=track",
                "apple_music": "https://music.apple.com/us/search?term=track",
                "discogs": "https://www.discogs.com/search?q=track",
            }),
            status,
            "2026-07-22T00:00:00Z",
        ),
    )


@pytest.mark.requirement("CAT-01")
def test_get_pending_tracks_returns_persisted_pending_rows_in_position_order(
    tmp_path: Path,
) -> None:
    client, conn = _make_client(tmp_path, playlist=_spotify_playlist())
    try:
        _insert_pending(conn, position=7)
        _insert_pending(conn, position=2)
        _insert_pending(conn, position=4, status="resolved")

        response = client.get("/api/v1/spotify/playlists/spotify:pl123/pending-tracks")

        assert response.status_code == 200
        rows = response.json()
        assert [row["position"] for row in rows] == [2, 7]
        assert rows[0]["spotify_uri"] == "spotify:track:2"
        assert rows[0]["suggested_sources"] == {
            "beatport": "https://www.beatport.com/search?q=track",
            "bandcamp": "https://bandcamp.com/search?q=track",
            "qobuz": "https://www.qobuz.com/us-en/search?q=track",
            "apple_music": "https://music.apple.com/us/search?term=track",
            "discogs": "https://www.discogs.com/search?q=track",
        }
    finally:
        client.close()
        conn.close()


@pytest.mark.requirement("CAT-01")
def test_get_pending_tracks_accepts_valid_source_keys_in_any_json_order(
    tmp_path: Path,
) -> None:
    client, conn = _make_client(tmp_path, playlist=_spotify_playlist())
    try:
        canonical_sources = {
            "beatport": "https://www.beatport.com/search?q=track",
            "bandcamp": "https://bandcamp.com/search?q=track",
            "qobuz": "https://www.qobuz.com/us-en/search?q=track",
            "apple_music": "https://music.apple.com/us/search?term=track",
            "discogs": "https://www.discogs.com/search?q=track",
        }
        _insert_pending(
            conn, position=0,
            sources=dict(reversed(list(canonical_sources.items()))),
        )

        response = client.get(
            "/api/v1/spotify/playlists/spotify:pl123/pending-tracks"
        )

        assert response.status_code == 200
        assert response.json()[0]["suggested_sources"] == canonical_sources
    finally:
        client.close()
        conn.close()


@pytest.mark.requirement("CAT-01")
@pytest.mark.parametrize("playlist", [None, Playlist(
    playlist_id="pl-001", name="Local Playlist", vendor="rekordbox",
)])
def test_get_pending_tracks_rejects_missing_or_non_spotify_playlist(
    tmp_path: Path,
    playlist: Playlist | None,
) -> None:
    client, conn = _make_client(tmp_path, playlist=playlist)
    try:
        response = client.get("/api/v1/spotify/playlists/pl-001/pending-tracks")

        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "SPOTIFY_PLAYLIST_NOT_FOUND"
    finally:
        client.close()
        conn.close()


@pytest.mark.requirement("CAT-01")
def test_get_pending_tracks_fails_when_auxiliary_table_is_absent(
    tmp_path: Path,
) -> None:
    client, conn = _make_client(
        tmp_path, playlist=_spotify_playlist(), with_pending_table=False,
    )
    try:
        response = client.get(
            "/api/v1/spotify/playlists/spotify:pl123/pending-tracks"
        )

        assert response.status_code == 500
        assert response.json()["detail"]["code"] == "PENDING_TRACKS_TABLE_MISSING"
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "pending_tracks" not in tables
    finally:
        client.close()
        conn.close()


@pytest.mark.requirement("CAT-01")
@pytest.mark.parametrize("sources", [
    {"beatport": "https://www.beatport.com/search?q=track"},
    {
        "beatport": "https://www.beatport.com/search?q=track",
        "bandcamp": "https://bandcamp.com/search?q=track",
        "qobuz": "https://www.qobuz.com/us-en/search?q=track",
        "apple_music": "https://music.apple.com/us/search?term=track",
        "discogs": 7,
    },
    "not-a-json-object",
])
def test_get_pending_tracks_fails_for_malformed_persisted_sources(
    tmp_path: Path,
    sources: object,
) -> None:
    client, conn = _make_client(tmp_path, playlist=_spotify_playlist())
    try:
        _insert_pending(conn, position=0, sources=sources)

        response = client.get(
            "/api/v1/spotify/playlists/spotify:pl123/pending-tracks"
        )

        assert response.status_code == 500
        assert response.json()["detail"]["code"] == "MALFORMED_PENDING_TRACK"
    finally:
        client.close()
        conn.close()

pytestmark = pytest.mark.rb_parity


def _account_client(opener: object) -> TestClient:
    app = FastAPI()
    app.state.spotify_account_opener = opener
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


@pytest.mark.requirement("SPOT-6555")
def test_production_app_registers_account_playlists() -> None:
    paths = create_app(mount_frontend=False).openapi()["paths"]
    assert "/api/v1/spotify/account/playlists" in paths
    assert "/api/v1/spotify/account/connect" in paths


def _real_opener(tmp_path: Path, *, api_base: str | None = None, token_url: str | None = None, signed_in: bool = True, expires_in: int = 3600):
    """The production opener on a token cache in tmp_path, aimed at loopback Spotify."""
    from apps.spotify import account
    from tests.spotify.spotify_loopback import write_token

    token = tmp_path / "token.json"
    if signed_in:
        write_token(token, expires_in=expires_in)

    def opener() -> object:
        return account.open_account_client(
            env={"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"},
            token_path=token,
            client_id_path=tmp_path / "cid.json",
            api_base=api_base,
            token_url=token_url,
        )

    return opener


@pytest.mark.requirement("SPOT-6555")
def test_account_playlists_lists_every_page(tmp_path: Path) -> None:
    from tests.spotify.spotify_loopback import WebApi, playlist

    with WebApi([playlist(i) for i in range(75)] + [playlist(900, owner="friend")]) as api:
        body = _account_client(_real_opener(tmp_path, api_base=api.api_base)).get(
            "/api/v1/spotify/account/playlists"
        ).json()
    assert body["connected"] is True
    assert body["user_id"] == "me"
    assert body["total_reported"] == 76
    assert len(body["playlists"]) == 76
    friend = body["playlists"][-1]
    assert friend["spotify_id"] == "pl0900" and friend["owned"] is False


@pytest.mark.requirement("SPOT-6555")
def test_account_playlists_not_signed_in_is_a_state_not_an_error(tmp_path: Path) -> None:
    resp = _account_client(_real_opener(tmp_path, signed_in=False)).get("/api/v1/spotify/account/playlists")
    assert resp.status_code == 200
    assert resp.json()["connected"] is False
    assert resp.json()["reason"] == "not_signed_in"
    assert resp.json()["playlists"] == []


def test_account_playlists_api_failure_is_502_and_401_asks_for_sign_in(tmp_path: Path) -> None:
    from tests.spotify.spotify_loopback import WebApi

    with WebApi([], fail_status=403) as api:
        resp = _account_client(_real_opener(tmp_path, api_base=api.api_base)).get("/api/v1/spotify/account/playlists")
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "SPOTIFY_API_ERROR"
    with WebApi([], fail_status=401) as api:
        resp = _account_client(_real_opener(tmp_path, api_base=api.api_base)).get("/api/v1/spotify/account/playlists")
    assert resp.status_code == 200
    assert resp.json()["reason"] == "token_refresh_failed"


def test_account_playlists_offline_opener_is_502_not_a_sign_in_prompt(tmp_path: Path) -> None:
    from tests.spotify.spotify_loopback import closed_port_url

    # An expired cached token forces a refresh, and nothing answers the token URL.
    opener = _real_opener(tmp_path, token_url=closed_port_url(), expires_in=-60)
    resp = _account_client(opener).get("/api/v1/spotify/account/playlists")
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "SPOTIFY_API_ERROR"


def test_a_damaged_client_id_file_is_a_500_not_a_setup_prompt(tmp_path: Path) -> None:
    """[if] the saved client id file is damaged [then] listing and Connect say so,
    [else] the panel offers setup and overwrites it."""
    from http.server import HTTPServer

    from apps.spotify import account

    cid = tmp_path / "cid.json"
    cid.write_text("{not json", encoding="utf-8", newline="\n")
    token = tmp_path / "token.json"
    app = FastAPI()
    app.state.spotify_client_id_path = cid
    app.state.spotify_account_opener = lambda: account.open_account_client(env={}, token_path=token, client_id_path=cid)

    def factory(_addr: tuple[str, int], handler: type) -> HTTPServer:
        return HTTPServer(("127.0.0.1", 0), handler)

    app.state.spotify_connect_flow = account.ConnectFlow(
        token_path=token, client_id_path=cid, server_factory=factory, timeout_s=1, env={}
    )
    app.include_router(router, prefix="/api/v1")
    client = TestClient(app)
    for resp in (client.get("/api/v1/spotify/account/playlists"), client.post("/api/v1/spotify/account/connect")):
        assert resp.status_code == 500
        assert resp.json()["detail"]["code"] == "SPOTIFY_CLIENT_ID_UNREADABLE"
        assert str(cid) in resp.json()["detail"]["message"]
    assert cid.read_text(encoding="utf-8") == "{not json"


@pytest.mark.requirement("SPOT-6555")
def test_a_fresh_install_saves_a_client_id_then_can_connect(tmp_path: Path) -> None:
    """[if] a fresh install has no env id and no saved id [then] the panel can
    save one and Connect starts with it, [else] sign-in can never bootstrap
   ."""
    from http.server import HTTPServer

    from apps.spotify import account

    # A fresh install: no SPOTIFY_CLIENT_ID (an empty environment, handed to the
    # production opener and ConnectFlow) and no saved id file.
    cid = tmp_path / "cid.json"
    token = tmp_path / "token.json"
    app = FastAPI()
    app.state.spotify_client_id_path = cid
    app.state.spotify_account_opener = lambda: account.open_account_client(
        env={}, token_path=token, client_id_path=cid
    )

    def factory(_addr: tuple[str, int], handler: type) -> HTTPServer:
        return HTTPServer(("127.0.0.1", 0), handler)

    app.state.spotify_connect_flow = account.ConnectFlow(
        token_path=token, client_id_path=cid, server_factory=factory, timeout_s=1, env={}
    )
    app.include_router(router, prefix="/api/v1")
    client = TestClient(app)

    assert client.get("/api/v1/spotify/account/playlists").json()["reason"] == "no_client_id"
    assert client.post("/api/v1/spotify/account/connect").json()["detail"]["code"] == "NO_CLIENT_ID"
    for bad in ("", "not-an-id", "g" * 32, "a" * 31):
        resp = client.put("/api/v1/spotify/account/client-id", json={"client_id": bad})
        assert resp.status_code == 422, bad
        assert resp.json()["detail"]["code"] == "SPOTIFY_CLIENT_ID_INVALID"
    assert not cid.exists()

    good = "0123456789ABCDEF0123456789abcdef"
    resp = client.put("/api/v1/spotify/account/client-id", json={"client_id": f"  {good}\n"})
    assert resp.status_code == 200 and resp.json() == {"saved": True}
    assert account.resolve_client_id({}, cid) == good.lower()

    assert client.get("/api/v1/spotify/account/playlists").json()["reason"] == "not_signed_in"
    url = client.post("/api/v1/spotify/account/connect").json()["authorize_url"]
    assert f"client_id={good.lower()}" in url


def _playlist_out(**extra: object) -> dict[str, object]:
    return {
        "spotify_id": "aaa", "name": "Invented aaa", "owner_id": "me", "owner_name": None,
        "owned": True, "collaborative": False, "public": None, "track_count": 3,
        "image_url": None, "open_url": "https://open.spotify.com/playlist/aaa", "snapshot_id": None,
        **extra,
    }


def test_the_listing_model_refuses_what_the_engine_never_returns() -> None:
    """[if] a connected listing lacks its total or user, or a row has an empty id [then] it is refused, [else stop].

    The engine always produces these; the response model is the contract, so
    a one-sided change fails here instead of reaching the panel."""
    good = {"connected": True, "user_id": "me", "display_name": None, "total_reported": 1,
            "playlists": [_playlist_out()]}
    # Controls: a complete listing, an empty account, and a disconnected answer.
    assert AccountPlaylistsOut(**good).total_reported == 1
    assert AccountPlaylistsOut(**{**good, "total_reported": 0, "playlists": []}).total_reported == 0
    assert (
        AccountPlaylistsOut(connected=False, reason="not_signed_in", message="Sign in", playlists=[]).total_reported
        is None
    )
    assert AccountPlaylistsOut(**{**good, "playlists": [_playlist_out(track_count=0)]}).playlists[0].track_count == 0
    assert AccountPlaylistsOut(**{**good, "playlists": [_playlist_out(track_count=None)]}).playlists[0].track_count is None
    for _label, body in [
        ("null total", {**good, "total_reported": None}),
        ("missing total", {k: v for k, v in good.items() if k != "total_reported"}),
        ("negative total", {**good, "total_reported": -1}),
        ("empty user", {**good, "user_id": ""}),
        ("empty playlist id", {**good, "playlists": [_playlist_out(spotify_id="")]}),
        ("null owner", {**good, "playlists": [_playlist_out(owner_id=None)]}),
        ("empty owner", {**good, "playlists": [_playlist_out(owner_id="")]}),
        ("disconnected without reason", {"connected": False, "message": "x", "playlists": []}),
        ("missing playlists", {k: v for k, v in good.items() if k != "playlists"}),
        ("disconnected without playlists", {"connected": False, "reason": "not_signed_in", "message": "x"}),
        # Sol P1 on #6555: a disconnected answer carries no rows and no account fields.
        ("disconnected with playlists", {"connected": False, "reason": "not_signed_in", "message": "x",
                                         "playlists": [_playlist_out()]}),
        ("disconnected with user", {"connected": False, "reason": "not_signed_in", "message": "x",
                                    "user_id": "me", "playlists": []}),
        ("disconnected with display name", {"connected": False, "reason": "not_signed_in", "message": "x",
                                            "display_name": "Me", "playlists": []}),
        ("disconnected with total", {"connected": False, "reason": "not_signed_in", "message": "x",
                                     "total_reported": 0, "playlists": []}),
        ("negative track count", {**good, "playlists": [_playlist_out(track_count=-1)]}),
        # Strict: nothing is coerced into a field's type.
        ("string total", {**good, "total_reported": "1"}),
        ("float total", {**good, "total_reported": 1.0}),
        ("string connected", {**good, "connected": "true"}),
        ("int collaborative", {**good, "playlists": [_playlist_out(collaborative=1)]}),
        ("string owned", {**good, "playlists": [_playlist_out(owned="yes")]}),
        ("string public", {**good, "playlists": [_playlist_out(public="false")]}),
        ("string track count", {**good, "playlists": [_playlist_out(track_count="3")]}),
        ("bool track count", {**good, "playlists": [_playlist_out(track_count=True)]}),
        ("int name", {**good, "playlists": [_playlist_out(name=7)]}),
        # The id is base-62 and the URL is the one built from it.
        # Its URL matches, so only the id check can refuse it.
        ("uri id", {**good, "playlists": [_playlist_out(
            spotify_id="spotify:playlist:aaa", open_url="https://open.spotify.com/playlist/spotify:playlist:aaa")]}),
        ("another playlist's url", {**good, "playlists": [_playlist_out(open_url="https://open.spotify.com/playlist/bbb")]}),
        ("album url", {**good, "playlists": [_playlist_out(open_url="https://open.spotify.com/album/aaa")]}),
        ("trailing slash url", {**good, "playlists": [_playlist_out(open_url="https://open.spotify.com/playlist/aaa/")]}),
    ]:
        with pytest.raises(ValidationError):
            AccountPlaylistsOut(**body)  # type: ignore[arg-type]

