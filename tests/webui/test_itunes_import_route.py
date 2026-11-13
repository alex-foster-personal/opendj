"""``POST /api/v1/import/itunes-xml``: the iTunes Library XML import without the UI (issue #9752, agent parity).

[if] the route does not import, report or refuse exactly as the adapter does [then] fail, [else stop].
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("DJIMPORT-14534")

FIXTURE = Path(__file__).parents[1] / "adapters" / "itunes_xml" / "Library.xml"
URL = "/api/v1/import/itunes-xml"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        writer.upsert_track(
            stable_id="t-alpha",
            stable_id_tier="inferred",
            title="Alpha",
            artists=[],
            album=None,
            isrc=None,
            duration_ms=200_000,
            file_path="/fixture/Music/Alpha One.mp3",
        )
    finally:
        writer.close()
        conn.close()
    app = create_app(backend=SqliteBackend(path), state_db_path=str(path), mount_frontend=False, enable_cors=False)
    try:
        yield TestClient(app)
    finally:
        playlist_write.close_store(app)


def test_route_imports_and_reports_unmatched(client: TestClient) -> None:
    """[if] the route does not write playlists or omits unmatched locations [then] fail, [else stop]."""
    response = client.post(URL, json={"source": str(FIXTURE.resolve()), "dry_run": False})
    assert response.status_code == 200
    body = response.json()
    assert (body["playlists_written"], body["folders_written"]) == (2, 1)
    assert "/fixture/Music/Missing.mp3" in body["unmatched_locations"]
    assert body["skipped"] == {"master": 1, "built_in": 1, "smart": 1}
    again = client.post(URL, json={"source": str(FIXTURE.resolve()), "dry_run": False}).json()
    assert again["playlist_ids"] == body["playlist_ids"]


def test_route_refuses_missing_relative_and_non_library_files(client: TestClient, tmp_path: Path) -> None:
    """[if] a missing, relative or non-Library file is accepted [then] fail, [else stop]."""
    assert client.post(URL, json={"source": "Library.xml", "dry_run": True}).status_code == 422
    missing = client.post(URL, json={"source": str(tmp_path / "none.xml"), "dry_run": True})
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "ITUNES_XML_NOT_FOUND"
    junk = tmp_path / "junk.xml"
    junk.write_text("<html/>", encoding="utf-8")
    bad = client.post(URL, json={"source": str(junk), "dry_run": True})
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "INVALID_ITUNES_XML"
