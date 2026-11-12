"""``opendj playlist summary`` mirrors the HTTP route (SETPREP-10645).

[if] CLI summary JSON drifts from GET /playlists/{id}/summary [then] fail, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.opendj_cli import EXIT_CONFIRMED, playlist_cli
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Playlist, Provenance, Track

pytestmark = pytest.mark.requirement("SETPREP-10645")

NOW = "2026-11-09T12:00:00Z"
SID = "f" * 40


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    backend = InMemoryBackend()
    backend.seed_track(
        Track(
            stable_id=SID,
            duration_ms=120_000,
            bpm=128.0,
            key="8A",
            provenance={"energy": Provenance(7, "mik", 1.0, NOW, "ok")},
            created_at=NOW,
            updated_at=NOW,
        )
    )
    backend.seed_playlist(Playlist(playlist_id="pl-cli", name="CLI", items=[SID]))
    db_path = tmp_path / "state.db"
    state_db.open_rw(db_path).close()
    return TestClient(
        create_app(
            backend=backend,
            state_db_path=str(db_path),
            bind_host="127.0.0.1",
            hostname="test-host",
            lock_status_fn=lambda: None,
            mount_frontend=False,
        )
    )


def test_playlist_summary_cli_json(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] opendj playlist summary --json differs from the route [then] fail, [else stop]."""
    expected = client.get("/api/v1/playlists/pl-cli/summary").json()

    def _handler(request: httpx.Request) -> httpx.Response:
        served = client.get(request.url.path)
        return httpx.Response(served.status_code, content=served.content, headers=dict(served.headers))

    class _Target:
        base_url = "http://127.0.0.1:9"

    real_client = httpx.Client
    monkeypatch.setattr(playlist_cli, "resolve_backend_base_url", lambda lock_path=None: _Target())
    monkeypatch.setattr(
        playlist_cli.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(_handler), **kw)
    )
    code = playlist_cli.run(["summary", "pl-cli"], as_json=True)
    assert code == EXIT_CONFIRMED
    printed = json.loads(capsys.readouterr().out)
    assert printed == expected
    assert playlist_cli.summary_path("pl 1/x") == "/api/v1/playlists/pl%201%2Fx/summary"
