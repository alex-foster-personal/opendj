"""DJIMPORT-10225: POST /api/v1/playlists/import and `opendj playlist import` build a playlist from an M3U.

[if] matched tracks lose file order, unmatched paths go unreported, or zero matches creates a playlist [then] fail, [else stop].
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.opendj_cli import api_cli, playlist_cli
from apps.opendj_cli.__main__ import main
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.waits import start_uvicorn_in_thread

pytestmark = [pytest.mark.requirement("DJIMPORT-10225")]

MUSIC = Path(os.path.normpath(os.path.abspath("/music/collection")))  # native: Windows adds the drive
TRACKS = {"t-001": MUSIC / "one.mp3", "t-002": MUSIC / "two.flac", "t-003": MUSIC / "three.wav"}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        for i, (sid, file_path) in enumerate(TRACKS.items(), start=1):
            writer.upsert_track(
                stable_id=sid,
                stable_id_tier="inferred",
                title=f"Track {i}",
                artists=[f"Artist {i}"],
                album=None,
                isrc=None,
                duration_ms=180_000 + i,
                file_path=str(file_path),
            )
    finally:
        writer.close()
        conn.close()
    return path


def _app(db_path: Path):  # type: ignore[no-untyped-def]
    return create_app(
        backend=SqliteBackend(db_path),
        state_db_path=str(db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    app = _app(db_path)
    with TestClient(app) as test_client:
        yield test_client
    playlist_write.close_store(app)


def _playlist_names(client: TestClient) -> list[str]:
    response = client.get("/api/v1/playlists")
    assert response.status_code == 200
    body = response.json()
    rows = body if isinstance(body, list) else body.get("playlists", body.get("items", []))
    return [row["name"] for row in rows]


def test_import_creates_a_playlist_in_file_order_and_reports_unmatched(client: TestClient) -> None:
    """[if] the import reorders matches or drops the unmatched list [then] fail, [else stop]."""
    m3u = "#EXTM3U\r\n#EXTINF:1,Three\r\nthree.wav\r\n/elsewhere/missing.mp3\r\none.mp3\r\nthree.wav\r\n"
    response = client.post("/api/v1/playlists/import", json={"name": "Imported", "m3u": m3u, "base_dir": str(MUSIC)})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["matched"] == 2
    assert body["unmatched"] == [os.path.normpath(os.path.abspath("/elsewhere/missing.mp3"))]
    detail = client.get(f"/api/v1/playlists/{body['playlist_id']}")
    assert detail.status_code == 200
    items = detail.json()["items"]
    stable_ids = [item if isinstance(item, str) else item["stable_id"] for item in items]
    assert stable_ids == ["t-003", "t-001"]


def test_zero_matches_creates_no_playlist_and_answers_422_with_the_count(client: TestClient) -> None:
    """[if] a file with no match creates a playlist or answers anything but 422 [then] fail, [else stop]."""
    before = _playlist_names(client)
    response = client.post(
        "/api/v1/playlists/import",
        json={"name": "Nothing", "m3u": "/nope/a.mp3\n/nope/b.mp3\n", "base_dir": str(MUSIC)},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["unmatched_count"] == 2
    assert _playlist_names(client) == before


@pytest.fixture
def library_daemon(db_path: Path) -> Iterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    app = _app(db_path)
    server, thread = start_uvicorn_in_thread(
        __import__("uvicorn").Config(app, log_level="warning"),
        what="the library daemon",
        sockets=[listener],
    )
    try:
        yield f"http://127.0.0.1:{int(listener.getsockname()[1])}"
    finally:
        server.should_exit = True
        thread.join(timeout=0.5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=10)
        playlist_write.close_store(app)


def test_cli_playlist_import_does_the_same_from_a_file(
    library_daemon: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] `opendj playlist import <file>` does not create the same playlist as the route [then] fail, [else stop]."""
    monkeypatch.setattr(
        playlist_cli,
        "resolve_backend_base_url",
        lambda environ=None, lock_path=None: api_cli._BackendTarget(base_url=library_daemon),
    )
    playlist = tmp_path / "Warmup.m3u8"
    playlist.write_text("".join(f"{TRACKS[sid]}\n" for sid in ("t-002", "t-001")) + "/nope/x.mp3\n")
    code = main(["--json", "playlist", "import", str(playlist)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["name"] == "Warmup"
    assert out["matched"] == 2
    assert out["unmatched"] == [os.path.normpath(os.path.abspath("/nope/x.mp3"))]
    detail = httpx.get(f"{library_daemon}/api/v1/playlists/{out['playlist_id']}", timeout=30).json()
    stable_ids = [item if isinstance(item, str) else item["stable_id"] for item in detail["items"]]
    assert stable_ids == ["t-002", "t-001"]
