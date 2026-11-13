"""LIBM-114: a playlist exports as a UTF-8 (with BOM) CSV set list, one row per track in playlist order.

[if] GET /api/v1/playlists/{id}/export.csv is not header plus rows in order, BOM-led and RFC 4180 quoted [then] fail, [else stop].
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, playlist_cli
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Playlist, Track
from apps.webui.server.playlist_csv_export import (
    CSV_EXPORT_COLUMNS,
    UTF8_BOM,
    CsvExportTrack,
    csv_export_filename,
    playlist_csv_export,
)

from .app_fixtures import _stub_rb_vendor

TRICKY_TITLE = 'Comma, "quote"\nand newline'

pytestmark = pytest.mark.requirement("LIBM-13849")


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    _stub_rb_vendor(monkeypatch)
    backend = InMemoryBackend()
    backend.seed_track(
        Track(
            stable_id="b",
            title="Second",
            artist="Bea",
            album="LP",
            genre="House",
            bpm=124.0,
            key="8A",
            duration_ms=241_400,
            rating=4,
            comments="peak",
            file_path="/music/b.flac",
        )
    )
    backend.seed_track(Track(stable_id="a", title=TRICKY_TITLE, artist="Café Å", bpm=128.456, duration_ms=59_600))
    backend.seed_track(Track(stable_id="c", title=None, artist=None))
    backend.seed_playlist(Playlist(playlist_id="pl-1", name="Sun / Set", vendor="djay", items=["b", "a", "c"]))
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


def _rows(body: bytes) -> list[list[str]]:
    assert body.startswith(UTF8_BOM)
    return list(csv.reader(io.StringIO(body[len(UTF8_BOM) :].decode("utf-8"), newline="")))


def test_three_track_playlist_exports_header_plus_three_rows_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] a playlist with 3 tracks is exported [then] the CSV has a header row plus 3 rows in playlist order, [else stop]."""
    with _client(monkeypatch, tmp_path) as client:
        response = client.get("/api/v1/playlists/pl-1/export.csv")
    assert response.status_code == 200, response.text
    rows = _rows(response.content)
    assert rows[0] == list(CSV_EXPORT_COLUMNS)
    assert rows[1:] == [
        ["1", "Second", "Bea", "LP", "House", "124.00", "8A", "241.400", "4", "peak", "/music/b.flac"],
        ["2", TRICKY_TITLE, "Café Å", "", "", "128.46", "", "59.600", "", "", ""],
        ["3", "", "", "", "", "", "", "", "", "", ""],
    ]


def test_comma_quote_and_newline_in_a_title_round_trip_through_csv_reader() -> None:
    """[if] a title holds a comma, a quote and a newline [then] csv.reader reads it back unchanged, [else stop]."""
    body = playlist_csv_export(
        [CsvExportTrack(TRICKY_TITLE, "x,y", None, None, None, None, None, None, 'say "hi"', None)]
    )
    rows = _rows(body)
    assert len(rows) == 2
    assert rows[1][1] == TRICKY_TITLE
    assert rows[1][2] == "x,y"
    assert rows[1][9] == 'say "hi"'
    assert b'"Comma, ""quote""\nand newline"' in body


def test_unknown_playlist_answers_404_with_a_typed_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] the playlist id does not exist [then] the route answers 404 with an error code and message, [else stop]."""
    with _client(monkeypatch, tmp_path) as client:
        response = client.get("/api/v1/playlists/nope/export.csv")
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["error"] == "not_found"
    assert "nope" in body["message"]


def test_response_is_bom_led_text_csv_named_after_the_playlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] the response is read [then] it is text/csv utf-8, starts with a BOM and is named <playlist>.csv, [else stop]."""
    with _client(monkeypatch, tmp_path) as client:
        response = client.get("/api/v1/playlists/pl-1/export.csv")
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert response.headers["content-disposition"] == 'attachment; filename="Sun _ Set.csv"'
    assert csv_export_filename("../..") == "playlist.csv"


def test_cli_export_csv_writes_the_route_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] opendj playlist export <id> --format csv runs [then] it writes the same bytes the route returns, [else stop]."""
    with _client(monkeypatch, tmp_path) as client:
        route_bytes = client.get("/api/v1/playlists/pl-1/export.csv").content

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
        out = tmp_path / "set.csv"
        assert playlist_cli.run(["export", "pl-1", "--format", "csv", "--out", str(out)]) == EXIT_CONFIRMED
        assert out.read_bytes() == route_bytes
        assert seen == ["/api/v1/playlists/pl-1/export.csv"]
        capsys.readouterr()
        assert playlist_cli.run(["export", "pl-1", "--format", "csv"], as_json=True) == EXIT_CONFIRMED
        payload = json.loads(capsys.readouterr().out)
        assert payload["format"] == "csv"
        assert payload["rows"] == 3
        assert payload["text"].encode("utf-8") == route_bytes
    assert playlist_cli.run(["export", "pl-1", "--format", "xls"], as_json=True) == EXIT_FAILED
    assert "usage" in capsys.readouterr().out
