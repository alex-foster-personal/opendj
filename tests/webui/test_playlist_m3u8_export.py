"""LIBORG-9748: a playlist exports as an extended M3U8 file (absolute paths, EXTINF lines) over HTTP and the CLI.

[if] GET /api/v1/playlists/{id}/export.m3u8 is not EXTM3U then EXTINF plus path per local track in order [then] fail, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, playlist_cli
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Playlist, Track
from apps.webui.server.playlist_m3u8_export import (
    M3u8ExportTrack,
    local_track_path,
    m3u8_export_filename,
    playlist_m3u8_export,
)

from .app_fixtures import _stub_rb_vendor

pytestmark = pytest.mark.requirement("LIBORG-14287")


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[TestClient, Path, Path]:
    _stub_rb_vendor(monkeypatch)
    music = tmp_path / "music"
    music.mkdir()
    first, second = music / "b.flac", music / "Café Å.mp3"
    first.write_bytes(b"x")
    second.write_bytes(b"x")
    backend = InMemoryBackend()
    backend.seed_track(Track(stable_id="b", title="Second", artist="Bea", duration_ms=241_400, file_path=str(first)))
    backend.seed_track(Track(stable_id="a", title="Café\nnight", artist=None, duration_ms=None, file_path=str(second)))
    backend.seed_track(Track(stable_id="s", title="Stream", artist="Web", file_path="https://tidal.example/track/1"))
    backend.seed_track(Track(stable_id="m", title="Gone", artist="Lost", file_path=str(music / "gone.wav")))
    backend.seed_playlist(Playlist(playlist_id="pl-1", name="Sun / Set", vendor="djay", items=["b", "s", "a", "m"]))
    (tmp_path / "state").mkdir()
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
        state_db_path=str(tmp_path / "state" / "state.db"),
    )
    return TestClient(app), first, second


def test_export_is_extm3u_then_extinf_and_path_per_track_in_playlist_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] a playlist is exported as M3U8 [then] it is #EXTM3U then #EXTINF:<s>,<artist> - <title> and path in order, [else stop]."""
    client, first, second = _client(monkeypatch, tmp_path)
    with client:
        response = client.get("/api/v1/playlists/pl-1/export.m3u8")
    assert response.status_code == 200, response.text
    lines = response.content.decode("utf-8").split("\n")
    assert lines[0] == "#EXTM3U"
    assert [line for line in lines if not line.startswith("# ")][1:] == [
        "#EXTINF:241,Bea - Second",
        str(first),
        "#EXTINF:-1,Café night",
        str(second),
        "",
    ]


def test_tracks_with_no_local_file_are_skipped_and_counted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] a playlist holds a streaming and a missing-file track [then] both are skipped, X-Skipped is 2, one comment says so, [else stop]."""
    client, _, _ = _client(monkeypatch, tmp_path)
    with client:
        response = client.get("/api/v1/playlists/pl-1/export.m3u8")
    text = response.content.decode("utf-8")
    assert response.headers["x-skipped"] == "2"
    assert text.split("\n")[1] == "# skipped 2 track(s) with no local file (streaming or missing)"
    assert "tidal.example" not in text and "gone.wav" not in text
    assert local_track_path(None) is None
    assert local_track_path("relative/a.mp3") is None
    assert (
        playlist_m3u8_export([M3u8ExportTrack("t", "a", 1000, "/x.mp3")]).body == b"#EXTM3U\n#EXTINF:1,a - t\n/x.mp3\n"
    )


def test_response_is_utf8_mpegurl_named_after_the_playlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] the response is read [then] it is audio/x-mpegurl utf-8, has no BOM and is named <playlist>.m3u8, [else stop]."""
    client, _, _ = _client(monkeypatch, tmp_path)
    with client:
        response = client.get("/api/v1/playlists/pl-1/export.m3u8")
        missing = client.get("/api/v1/playlists/nope/export.m3u8")
    assert response.headers["content-type"] == "audio/x-mpegurl; charset=utf-8"
    assert response.content.startswith(b"#EXTM3U\n")
    assert response.headers["content-disposition"] == 'attachment; filename="Sun _ Set.m3u8"'
    assert m3u8_export_filename("../..") == "playlist.m3u8"
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"


def test_cli_export_m3u8_writes_the_route_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] opendj playlist export <id> --format m3u8 --out runs [then] it writes the same bytes the route returns, [else stop]."""
    client, _, _ = _client(monkeypatch, tmp_path)
    with client:
        route_bytes = client.get("/api/v1/playlists/pl-1/export.m3u8").content

        class _Target:
            base_url = "http://engine.test"

        seen: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            served = client.get(request.url.path)
            return httpx.Response(served.status_code, content=served.content, headers=dict(served.headers))

        real_client = httpx.Client
        monkeypatch.setattr(playlist_cli, "resolve_backend_base_url", lambda lock_path=None: _Target())
        monkeypatch.setattr(
            playlist_cli.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(_handler), **kw)
        )
        out = tmp_path / "set.m3u8"
        assert playlist_cli.run(["export", "pl-1", "--format", "m3u8", "--out", str(out)]) == EXIT_CONFIRMED
        assert out.read_bytes() == route_bytes
        assert seen == ["/api/v1/playlists/pl-1/export.m3u8"]
        capsys.readouterr()
        assert playlist_cli.run(["export", "pl-1", "--format", "m3u8"], as_json=True) == EXIT_CONFIRMED
        payload = json.loads(capsys.readouterr().out)
        assert (payload["format"], payload["rows"], payload["skipped"]) == ("m3u8", 2, 2)
        assert payload["text"].encode("utf-8") == route_bytes
    assert playlist_cli.run(["export", "pl-1", "--format", "pls"], as_json=True) == EXIT_FAILED
    assert "usage" in capsys.readouterr().out
