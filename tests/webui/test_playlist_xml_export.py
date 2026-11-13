"""LIBORG-9751: one playlist exports as rekordbox XML over HTTP and the CLI, and the reader reads it back.

[if] GET /api/v1/playlists/{id}/export.xml does not round-trip the playlist's tracks, BPM, key and order [then] fail, [else stop].
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.adapters.rekordbox_xml.reader import read_rekordbox_xml
from apps.opendj_cli import EXIT_CONFIRMED, playlist_cli
from apps.webui.server.routes import playlist_xml_export
from tests.adapters.rekordbox_xml.export_fixtures import (
    MUSIC,
    add_members,
    add_node,
    add_raw_track,
    delete_tracks,
    make_tree_library,
)

pytestmark = pytest.mark.requirement("LIBORG-14300")


def _client(db: Path) -> TestClient:
    app = FastAPI()
    app.state.state_db_path = db
    app.include_router(playlist_xml_export.router)
    return TestClient(app)


def _read_back(tmp_path: Path, body: bytes):
    out = tmp_path / "playlist.xml"
    out.write_bytes(body)
    return read_rekordbox_xml(out)


def test_playlist_round_trips_through_the_reader_with_same_tracks_bpm_key_and_order(tmp_path: Path) -> None:
    """[if] one playlist is exported [then] the reader reads back only its tracks, in order, with BPM and key, [else stop]."""
    db, ids = make_tree_library(tmp_path)
    response = _client(db).get("/playlists/p1/export.xml")
    assert response.status_code == 200, response.text
    parsed = _read_back(tmp_path, response.content)
    tracks = {t.track_id: t for t in parsed.library.tracks}
    assert sorted(t.file_path for t in tracks.values()) == sorted(f"{MUSIC}/{sid} #1 %20.mp3" for sid in ids[:3])
    (playlist,) = parsed.library.playlists
    assert playlist.name == "Warmup"
    stems = [tracks[tid].file_path.rsplit("/", 1)[1].split(" ")[0] for tid in playlist.track_ids]
    assert stems == [ids[2], ids[0], ids[1]]
    first = next(t for t in tracks.values() if ids[0] in t.file_path)
    assert (first.bpm, first.key_camelot) == (120.5, "8A")


def test_document_is_dj_playlists_1_0_0_with_percent_encoded_file_uri(tmp_path: Path) -> None:
    """[if] the export is parsed as XML [then] it is DJ_PLAYLISTS 1.0.0 with PRODUCT and a file:// URI Location, [else stop]."""
    db, ids = make_tree_library(tmp_path)
    root = ET.fromstring(_client(db).get("/playlists/p2/export.xml").content)
    assert (root.tag, root.get("Version")) == ("DJ_PLAYLISTS", "1.0.0")
    assert [child.tag for child in root] == ["PRODUCT", "COLLECTION", "PLAYLISTS"]
    locations = {t.get("Location") for t in root.iter("TRACK") if t.get("Location")}
    assert f"file://localhost/Volumes/Invented%20DJ/Music/{ids[4]}%20%231%20%2520.mp3" in locations


def test_a_track_twice_in_the_playlist_is_one_collection_entry_and_two_playlist_entries(tmp_path: Path) -> None:
    """[if] a playlist holds a track twice [then] COLLECTION lists it once and the playlist keeps both entries, [else stop]."""
    db, ids = make_tree_library(tmp_path)
    add_node(db, "p3", "Repeat", "playlist", None, 9)
    add_members(db, "p3", [ids[0], ids[1], ids[0]])
    response = _client(db).get("/playlists/p3/export.xml")
    assert (response.headers["x-export-tracks"], response.headers["x-export-entries"]) == ("2", "3")
    (playlist,) = _read_back(tmp_path, response.content).library.playlists
    assert len(playlist.track_ids) == 3 and playlist.track_ids[0] == playlist.track_ids[2]


def test_pathless_streaming_and_deleted_members_are_left_out_and_counted(tmp_path: Path) -> None:
    """[if] a playlist holds pathless, streaming and deleted tracks [then] they are left out and counted in headers, [else stop]."""
    db, ids = make_tree_library(tmp_path)
    add_raw_track(db, "web", "https://tidal.example/track/1")
    add_raw_track(db, "nopath", "")
    delete_tracks(db, {ids[1]: "user"})
    add_node(db, "p4", "Mixed", "playlist", None, 9)
    add_members(db, "p4", [ids[0], "web", "nopath", ids[1]])
    response = _client(db).get("/playlists/p4/export.xml")
    assert response.status_code == 200, response.text
    headers = response.headers
    assert (
        headers["x-export-tracks"],
        headers["x-export-skipped-streaming"],
        headers["x-export-skipped-no-location"],
    ) == (
        "1",
        "1",
        "1",
    )
    assert len(_read_back(tmp_path, response.content).library.tracks) == 1


def test_unknown_id_is_404_and_a_folder_is_refused_with_409(tmp_path: Path) -> None:
    """[if] the id is unknown or names a folder [then] the route answers 404 or 409 with a typed code, [else stop]."""
    db, _ = make_tree_library(tmp_path)
    client = _client(db)
    missing = client.get("/playlists/nope/export.xml")
    folder = client.get("/playlists/f1/export.xml")
    assert (missing.status_code, missing.json()["detail"]["code"]) == (404, "PLAYLIST_NOT_FOUND")
    assert (folder.status_code, folder.json()["detail"]["code"]) == (409, "PLAYLIST_EXPORT_REFUSED")


def test_cli_export_rekordbox_xml_writes_the_route_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] opendj playlist export <id> --format rekordbox-xml --out runs [then] it writes the route bytes, [else stop]."""
    db, _ = make_tree_library(tmp_path)
    client = _client(db)
    route_bytes = client.get("/playlists/p1/export.xml").content

    class _Target:
        base_url = "http://engine.test"

    seen: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        served = client.get(request.url.path.removeprefix("/api/v1"))
        return httpx.Response(served.status_code, content=served.content, headers=dict(served.headers))

    real_client = httpx.Client
    monkeypatch.setattr(playlist_cli, "resolve_backend_base_url", lambda lock_path=None: _Target())
    monkeypatch.setattr(
        playlist_cli.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(_handler), **kw)
    )
    out = tmp_path / "set.xml"
    assert playlist_cli.run(["export", "p1", "--format", "rekordbox-xml", "--out", str(out)]) == EXIT_CONFIRMED
    assert out.read_bytes() == route_bytes
    assert seen == ["/api/v1/playlists/p1/export.xml"]
    capsys.readouterr()
    assert playlist_cli.run(["export", "p1", "--format", "rekordbox-xml"], as_json=True) == EXIT_CONFIRMED
    payload = json.loads(capsys.readouterr().out)
    assert (payload["format"], payload["rows"]) == ("rekordbox-xml", 3)
