"""``opendj api POST .../items:add`` agent-native parity (LIBM-20)."""
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


def test_api_post_items_add_duplicate_allowed(
    library_daemon: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base_url, _db = library_daemon
    monkeypatch.setattr(
        api_cli,
        "resolve_backend_base_url",
        lambda environ=None, lock_path=None: api_cli._BackendTarget(base_url=base_url),
    )
    import httpx

    create = httpx.post(
        f"{base_url}/api/v1/playlists", json={"name": "CLI Set"}, timeout=30,
    )
    assert create.status_code == 201
    pid = create.json()["playlist_id"]

    code = main([
        "api", "POST", f"/api/v1/playlists/{pid}/items:add",
        "--json", '{"stable_ids":["t-004"]}',
    ])
    assert code == api_cli.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert [row["stable_id"] for row in out["added"]] == ["t-004"]

    code2 = main([
        "api", "POST", f"/api/v1/playlists/{pid}/items:add",
        "--json", '{"stable_ids":["t-004"]}',
    ])
    assert code2 == api_cli.EXIT_OK
    out2 = json.loads(capsys.readouterr().out)
    assert [row["stable_id"] for row in out2["added"]] == ["t-004"]
    assert out2["added"][0]["item_id"] != out["added"][0]["item_id"]
    detail = httpx.get(f"{base_url}/api/v1/playlists/{pid}", timeout=30).json()
    assert detail["items"].count("t-004") == 2


def test_api_post_items_add_noop_when_forbid_duplicates(
    library_daemon: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base_url, _db = library_daemon
    monkeypatch.setattr(
        api_cli,
        "resolve_backend_base_url",
        lambda environ=None, lock_path=None: api_cli._BackendTarget(base_url=base_url),
    )
    import httpx

    create = httpx.post(
        f"{base_url}/api/v1/playlists", json={"name": "CLI Forbid"}, timeout=30,
    )
    assert create.status_code == 201
    pid = create.json()["playlist_id"]
    etag = create.headers["etag"]

    code = main([
        "api", "POST", f"/api/v1/playlists/{pid}/items:add",
        "--json", '{"stable_ids":["t-004"]}',
    ])
    assert code == api_cli.EXIT_OK
    capsys.readouterr()
    etag_hdr = httpx.get(
        f"{base_url}/api/v1/playlists/{pid}", timeout=30,
    ).headers["etag"]

    patch_code = main([
        "api", "PATCH", f"/api/v1/playlists/{pid}",
        "-H", f"If-Match: {etag_hdr}",
        "--json", '{"forbid_duplicates":true}',
    ])
    assert patch_code == api_cli.EXIT_OK
    patch_out = json.loads(capsys.readouterr().out)

    code2 = main([
        "api", "POST", f"/api/v1/playlists/{pid}/items:add",
        "--json", '{"stable_ids":["t-004"]}',
    ])
    assert code2 == api_cli.EXIT_OK
    out2 = json.loads(capsys.readouterr().out)
    assert out2["added"] == []
    detail = httpx.get(f"{base_url}/api/v1/playlists/{pid}", timeout=30).json()
    assert detail["items"].count("t-004") == 1
    assert patch_out["forbid_duplicates"] is True
