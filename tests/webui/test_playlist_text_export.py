"""LIBORG-10314: a playlist exports as a tab-separated UTF-8 text track list (rekordbox .txt export).

[if] GET /api/v1/playlists/{id}/export.txt does not return the header row then one row per track in playlist order [then] fail, [else stop].
[if] a tab or newline inside a field changes a row's column count [then] fail, [else stop].
[if] `opendj playlist export <id> --format txt` does not write the same bytes as the route [then] fail, [else stop].
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
from apps.webui.server.playlist_text_export import TextExportTrack, export_filename, playlist_text_export

from .app_fixtures import _stub_rb_vendor

pytestmark = [pytest.mark.requirement("LIBORG-10314")]

HEADER = "#\tTitle\tArtist\tBPM\tKey\tTime"


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    _stub_rb_vendor(monkeypatch)
    backend = InMemoryBackend()
    backend.seed_track(Track(stable_id="b", title="Second", artist="Bea", bpm=124.0, key="8A", duration_ms=241_400))
    backend.seed_track(
        Track(stable_id="a", title="Tab\there\nnew line", artist="Café Å", bpm=128.456, key="11B", duration_ms=59_600)
    )
    backend.seed_track(Track(stable_id="c", title=None, artist=None))
    backend.seed_playlist(Playlist(playlist_id="pl-1", name="Sun / Set", vendor="djay", items=["b", "a", "c"]))
    backend.seed_playlist(Playlist(playlist_id="pl-empty", name="Empty", vendor="djay", items=[]))
    (tmp_path / "state").mkdir()
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
        state_db_path=str(tmp_path / "state" / "state.db"),
    )
    return TestClient(app)


def test_route_returns_header_then_rows_in_playlist_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] the export route skips the header, reorders rows or is not UTF-8 tab-separated [then] fail."""
    with _client(monkeypatch, tmp_path) as client:
        response = client.get("/api/v1/playlists/pl-1/export.txt")
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/plain; charset=utf-8"
        assert response.headers["content-disposition"] == 'attachment; filename="Sun _ Set.txt"'
        lines = response.content.decode("utf-8").split("\n")
        assert lines == [
            HEADER,
            "1\tSecond\tBea\t124.00\t8A\t4:01",
            "2\tTab here new line\tCafé Å\t128.46\t11B\t1:00",
            "3\t\t\t\t\t",
            "",
        ]
        assert client.get("/api/v1/playlists/pl-empty/export.txt").text == HEADER + "\n"
        assert client.get("/api/v1/playlists/nope/export.txt").status_code == 404


def test_tabs_and_newlines_in_fields_keep_every_row_the_same_width() -> None:
    """[if] a tab, CR or newline inside a field adds a column or a row [then] fail."""
    text = playlist_text_export(
        [
            TextExportTrack(title="a\tb\r\nc", artist="x\ny", bpm=None, key="1\tA", duration_ms=None),
            TextExportTrack(title="plain", artist="p", bpm=120, key="2A", duration_ms=0),
        ]
    )
    rows = text.rstrip("\n").split("\n")
    assert len(rows) == 3
    assert {row.count("\t") for row in rows} == {5}
    assert rows[1] == "1\ta b  c\tx y\t\t1 A\t"
    assert rows[2] == "2\tplain\tp\t120.00\t2A\t0:00"
    assert export_filename("../..") == "playlist.txt"


def test_cli_export_writes_the_route_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] `opendj playlist export <id> --format txt` differs from the route or skips --format [then] fail."""
    with _client(monkeypatch, tmp_path) as client:
        route_bytes = client.get("/api/v1/playlists/pl-1/export.txt").content

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
        out = tmp_path / "set.txt"
        assert playlist_cli.run(["export", "pl-1", "--format", "txt", "--out", str(out)]) == EXIT_CONFIRMED
        assert out.read_bytes() == route_bytes
        assert seen == ["/api/v1/playlists/pl-1/export.txt"]
        capsys.readouterr()
        assert playlist_cli.run(["export", "pl-1", "--format", "txt"], as_json=True) == EXIT_CONFIRMED
        payload = json.loads(capsys.readouterr().out)
        assert payload["rows"] == 3
        assert payload["text"].encode("utf-8") == route_bytes
    assert playlist_cli.export_path("pl 1/x", "txt") == "/api/v1/playlists/pl%201%2Fx/export.txt"
    assert playlist_cli.run(["export", "pl-1"], as_json=True) == EXIT_FAILED
    assert "usage" in capsys.readouterr().out
