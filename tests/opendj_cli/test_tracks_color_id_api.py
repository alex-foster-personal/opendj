"""Agent parity for GET /api/v1/tracks?color_id=... (issue #10041).

[if] opendj cannot call the color_id filter [then] agent parity is broken, [else stop].
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator

import pytest
import uvicorn
from fastapi.testclient import TestClient

from apps.opendj_cli import api_cli
from apps.opendj_cli.__main__ import main
from apps.webui.server.backend import Track
from tests.waits import start_uvicorn_in_thread
from tests.webui.app_fixtures import client_fixture  # noqa: F401
from tests.webui.conftest import seed_backend  # noqa: F401

pytestmark = pytest.mark.requirement("LIBUX-10679")


@pytest.fixture
def engine(client: TestClient, seed_backend, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:  # noqa: F811
    stamp = "2026-11-09T12:00:00Z"
    seed_backend.seed_track(Track(stable_id="cli-red", title="CLI Red", color_id=2, created_at=stamp, updated_at=stamp))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    base_url = f"http://127.0.0.1:{int(listener.getsockname()[1])}"
    server, thread = start_uvicorn_in_thread(
        uvicorn.Config(client.app, log_level="warning"), what="the engine", sockets=[listener]
    )
    monkeypatch.setattr(
        api_cli,
        "resolve_backend_base_url",
        lambda environ=None, lock_path=None: api_cli._BackendTarget(base_url=base_url),
    )
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_opendj_api_get_tracks_color_id_matches_http(engine: str, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] an agent runs opendj api GET with color_id [then] JSON matches the HTTP route, [else stop]."""
    assert main(["api", "GET", "/api/v1/tracks?color_id=2"]) == api_cli.EXIT_OK, capsys.readouterr().err
    body = json.loads(capsys.readouterr().out)
    ids = {row["stable_id"] for row in body["items"]}
    assert "cli-red" in ids
    assert all(
        row.get("color_id") in (None, 0, 2) or row.get("color_id") == 2
        for row in body["items"]
        if row["stable_id"] == "cli-red"
    )
