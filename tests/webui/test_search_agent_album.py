"""Agent parity for whole-collection album search (issue #10037)."""

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
from apps.webui import port_config
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.waits import start_uvicorn_in_thread


@pytest.fixture
def album_search_daemon(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    state_path = tmp_path / "state.db"
    conn = state_db.open_rw(state_path)
    bus = FakeEventBus()
    writer = StateWriter(conn, bus=bus, actor="test")
    try:
        writer.upsert_track(
            stable_id="s-agent",
            stable_id_tier="inferred",
            title="Lantern Orchard",
            artists=["Mira Valen"],
            album="Kestrelmoor Sessions",
            isrc=None,
            duration_ms=200_000,
            file_path="/music/s-agent.mp3",
        )
    finally:
        writer.close()
        conn.close()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = int(listener.getsockname()[1])
    app = create_app(
        backend=SqliteBackend(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_path),
        mount_frontend=False,
    )
    server, thread = start_uvicorn_in_thread(
        __import__("uvicorn").Config(app, log_level="warning"),
        what="the album search daemon",
        sockets=[listener],
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url, state_path
    finally:
        server.should_exit = True
        thread.join(timeout=0.5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=10)


@pytest.mark.requirement("LIBUX-10721")
def test_opendj_api_get_search_matches_album(
    album_search_daemon: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] opendj api GET /api/v1/search?q=albumword [then] JSON lists the track, [else stop]."""
    base_url, _ = album_search_daemon
    monkeypatch.delenv("MUSIC_DJ_BACKEND_PORT", raising=False)
    monkeypatch.delenv("MUSIC_DJ_FRONTEND_PORT", raising=False)
    monkeypatch.setattr(port_config, "WEBUI_ENV_FILE", tmp_path / "no.env")
    monkeypatch.setattr(
        api_cli,
        "resolve_backend_base_url",
        lambda environ=None, lock_path=None: api_cli._BackendTarget(base_url=base_url),
    )

    assert main(["--json", "api", "GET", "/api/v1/search?q=kestrelmoor&limit=5"]) == api_cli.EXIT_OK
    body = json.loads(capsys.readouterr().out)
    assert body["total"] == 1
    assert body["items"][0]["stable_id"] == "s-agent"
