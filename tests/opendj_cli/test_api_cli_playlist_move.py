"""``opendj api POST .../items:move`` agent-native parity (LIBM-22)."""
from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.opendj_cli import api_cli
from apps.opendj_cli.__main__ import main
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.waits import start_uvicorn_in_thread

TRACK_IDS = ["t-001", "t-002", "t-003", "t-004"]


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        for i, sid in enumerate(TRACK_IDS, start=1):
            writer.upsert_track(
                stable_id=sid, stable_id_tier="inferred",
                title=f"Track {i}", artists=[f"Artist {i}"], album=None,
                isrc=None, duration_ms=180_000 + i, file_path=None,
            )
    finally:
        writer.close()
        conn.close()
    return path


@pytest.fixture
def library_daemon(seeded_db: Path) -> Iterator[tuple[str, Path]]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    app = create_app(
        backend=SqliteBackend(seeded_db),
        state_db_path=str(seeded_db),
        mount_frontend=False,
        enable_cors=False,
    )
    server, thread = start_uvicorn_in_thread(
        __import__("uvicorn").Config(app, log_level="warning"),
        what="the library daemon",
        sockets=[listener],
    )
    port = int(listener.getsockname()[1])
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url, seeded_db
    finally:
        server.should_exit = True
        thread.join(timeout=0.5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=10)
        playlist_write.close_store(app)


def test_api_post_items_move(
    library_daemon: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base_url, _db = library_daemon
    monkeypatch.setattr(
        api_cli, "resolve_backend_base_url", lambda environ=None: base_url,
    )
    import httpx

    create = httpx.post(
        f"{base_url}/api/v1/playlists", json={"name": "CLI Set"}, timeout=30,
    )
    assert create.status_code == 201
    pid = create.json()["playlist_id"]
    etag = create.headers["ETag"]
    put = httpx.put(
        f"{base_url}/api/v1/playlists/{pid}/tracks",
        json={"stable_ids": TRACK_IDS},
        headers={"If-Match": etag},
        timeout=30,
    )
    assert put.status_code == 200
    etag = put.headers["ETag"]
    detail = httpx.get(f"{base_url}/api/v1/playlists/{pid}", timeout=30)
    ids = [t["item_id"] for t in detail.json()["tracks"]]

    code = main([
        "api", "POST", f"/api/v1/playlists/{pid}/items:move",
        "-H", f"If-Match: {etag}",
        "--json", json.dumps({
            "range_start": ids[0],
            "range_length": 2,
            "range_end": ids[1],
            "after_item_id": ids[3],
        }),
    ])
    assert code == api_cli.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["items"] == ["t-003", "t-004", "t-001", "t-002"]
    assert out["renumbered"] is False

    code2 = main([
        "api", "POST", f"/api/v1/playlists/{pid}/items:move",
        "-H", f"If-Match: {etag}",
        "--json", json.dumps({
            "range_start": ids[0],
            "range_length": 2,
            "range_end": ids[1],
            "after_item_id": ids[3],
        }),
    ])
    assert code2 == api_cli.EXIT_CONFLICT
    err = json.loads(capsys.readouterr().err)
    assert err["error"] == "conflict"

    fresh = httpx.get(f"{base_url}/api/v1/playlists/{pid}", timeout=30)
    fresh_etag = fresh.headers["ETag"]
    code3 = main([
        "api", "POST", f"/api/v1/playlists/{pid}/items:move",
        "-H", f"If-Match: {fresh_etag}",
        "--json", json.dumps({
            "range_start": ids[0],
            "range_length": 2,
            "range_end": ids[2],
            "after_item_id": ids[3],
        }),
    ])
    assert code3 == api_cli.EXIT_FAILED
    err3 = json.loads(capsys.readouterr().err)
    assert err3["error"] == "slice_not_contiguous"
