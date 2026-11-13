"""``opendj taglist`` drives the Tag List routes without the UI (issue #10028, agent parity).

[if] an opendj taglist verb does not reach its /api/v1/taglist route [then] fail, [else stop].
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.opendj_cli import EXIT_FAILED, api_cli, taglist_cli
from apps.opendj_cli.__main__ import main
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("LIBORG-14510")

TRACK_IDS = ["t-001", "t-002", "t-003"]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        for i, sid in enumerate(TRACK_IDS, start=1):
            writer.upsert_track(
                stable_id=sid,
                stable_id_tier="inferred",
                title=f"Track {i}",
                artists=[f"Artist {i}"],
                album=None,
                isrc=None,
                duration_ms=180_000 + i,
                file_path=None,
            )
    finally:
        writer.close()
        conn.close()
    app = create_app(backend=SqliteBackend(path), state_db_path=str(path), mount_frontend=False, enable_cors=False)
    try:
        yield TestClient(app)
    finally:
        playlist_write.close_store(app)


@pytest.fixture
def routed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def _handler(request: httpx.Request) -> httpx.Response:
        served = client.request(
            request.method, request.url.path, content=request.content, headers=dict(request.headers)
        )
        return httpx.Response(served.status_code, content=served.content, headers=dict(served.headers))

    real_client = httpx.Client
    monkeypatch.setattr(
        api_cli,
        "resolve_backend_base_url",
        lambda environ=None, lock_path=None: api_cli._BackendTarget(base_url="http://127.0.0.1:9"),
    )
    monkeypatch.setattr(
        api_cli.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(_handler), **kw)
    )
    return client


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main(["--json", "taglist", *argv])
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else {})


def test_cli_add_list_remove_save_clear(routed: TestClient, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] any opendj taglist verb disagrees with its route [then] fail, [else stop]."""
    code, added = _cli(capsys, "add", "t-002", "t-001", "t-002")
    assert code == api_cli.EXIT_OK
    assert added["added"] == ["t-002", "t-001"]
    code, listed = _cli(capsys, "list")
    assert code == api_cli.EXIT_OK
    assert listed == routed.get("/api/v1/taglist").json()
    assert [row["stable_id"] for row in listed["items"]] == ["t-002", "t-001"]  # type: ignore[union-attr, index]
    code, saved = _cli(capsys, "save", "Prep", "Fri")
    assert code == api_cli.EXIT_OK
    assert saved["name"] == "Prep Fri"
    assert routed.get(f"/api/v1/playlists/{saved['playlist_id']}").json()["items"] == ["t-002", "t-001"]
    code, _ = _cli(capsys, "remove", "t-002")
    assert code == api_cli.EXIT_OK
    code, cleared = _cli(capsys, "clear")
    assert code == api_cli.EXIT_OK
    assert cleared == {"removed": 1}
    assert routed.get("/api/v1/taglist").json()["count"] == 0


def test_cli_bad_verb_is_usage_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] a wrong verb or missing argument is sent to the route instead of a usage error [then] fail, [else stop]."""
    assert taglist_cli.taglist_request("remove", []) is None
    assert taglist_cli.taglist_request("list", ["x"]) is None
    assert taglist_cli.taglist_request("remove", ["a/b"]) == ["DELETE", "/api/v1/taglist/items/a%2Fb"]
    assert taglist_cli.run(["frobnicate"], as_json=True) == EXIT_FAILED
