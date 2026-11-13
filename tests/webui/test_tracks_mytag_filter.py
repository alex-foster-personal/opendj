"""My Tag filter on the tracks list and playlist detail (issue #9742).

- [if] GET /tracks?mytag=a&mytag=b&mytag_mode=or keeps a row holding neither [then] OR matching lies, [else stop].
- [if] mytag_mode=and keeps a row holding only one of the picks [then] AND matching lies, [else stop].
- [if] a pick matches only one letter case or width [then] the API and the tag catalog disagree on identity, [else stop].
- [if] a blank mytag or an unknown mytag_mode is accepted [then] a typo silently changes the list, [else stop].
- [if] opendj api GET with mytag differs from HTTP [then] agent parity is broken, [else stop].
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from apps.opendj_cli import api_cli
from apps.opendj_cli.__main__ import main
from apps.webui.server.backend import InMemoryBackend, TrackFilter
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.waits import start_uvicorn_in_thread
from tests.webui.app_fixtures import client_fixture  # noqa: F401
from tests.webui.conftest import seed_backend  # noqa: F401
from tests.webui.test_sqlite_backend import fresh_state_db  # noqa: F401

# "WARMUP" in full-width letters, written as code points so the source stays ASCII.
FULL_WIDTH_WARMUP = "".join(map(chr, (0xFF37, 0xFF21, 0xFF32, 0xFF2D, 0xFF35, 0xFF30)))

# The same tag sets the frontend My Tag funnel test filters, so the API and the UI agree.
TAGS = {
    "track-001": ["Warmup", "Vocal"],
    "track-002": ["peak"],
    "track-003": ["WARMUP"],
    "track-004": ["vocal", "Peak"],
    "track-005": [],
}


@pytest.fixture(autouse=True)
def _tags(seed_backend: InMemoryBackend) -> None:  # noqa: F811
    """Give the seeded tracks My Tags in mixed letter case; track-005 holds none."""
    for sid, tags in TAGS.items():
        seed_backend._tracks[sid].tags = list(tags)


def _ids(rows: list[dict]) -> set[str]:
    return {row["stable_id"] for row in rows}


def test_or_keeps_rows_holding_any_pick(client: TestClient) -> None:
    """[if] GET /tracks gets mytag=warmup&mytag=peak with or [then] every row holding either comes back, [else stop]."""
    response = client.get("/api/v1/tracks", params=[("mytag", "warmup"), ("mytag", "peak"), ("mytag_mode", "or")])
    assert response.status_code == 200, response.text
    assert _ids(response.json()["items"]) == {"track-001", "track-002", "track-003", "track-004"}


def test_and_keeps_only_rows_holding_every_pick(client: TestClient) -> None:
    """[if] GET /tracks gets mytag=vocal&mytag=peak with and [then] only the row holding both comes back, [else stop]."""
    response = client.get("/api/v1/tracks", params=[("mytag", "vocal"), ("mytag", "peak"), ("mytag_mode", "and")])
    assert response.status_code == 200, response.text
    assert _ids(response.json()["items"]) == {"track-004"}


def test_default_mode_is_or_and_no_pick_keeps_every_row(client: TestClient) -> None:
    """[if] mytag_mode is left out or no mytag is sent [then] the picks match any, and no picks keep all, [else stop]."""
    response = client.get("/api/v1/tracks", params=[("mytag", "VOCAL"), ("mytag", "peak")])
    assert _ids(response.json()["items"]) == {"track-001", "track-002", "track-004"}
    assert len(client.get("/api/v1/tracks", params={"mytag_mode": "and"}).json()["items"]) == 5


def test_pick_matches_the_catalog_identity_ignoring_case_and_width(client: TestClient) -> None:
    """[if] a pick is sent as full-width upper case [then] it matches the tag like the tag catalog does, [else stop]."""
    response = client.get("/api/v1/tracks", params={"mytag": FULL_WIDTH_WARMUP + " "})
    assert _ids(response.json()["items"]) == {"track-001", "track-003"}


def test_playlist_detail_filters_rows_by_mytag_and_keeps_membership(client: TestClient) -> None:
    """[if] GET /playlists/{id} gets mytag picks [then] tracks narrow by the same rule and items stay full, [else stop]."""
    detail = client.get(
        "/api/v1/playlists/pl-002", params=[("mytag", "vocal"), ("mytag", "peak"), ("mytag_mode", "and")]
    ).json()
    assert [row["stable_id"] for row in detail["tracks"]] == ["track-004"]
    assert detail["items"] == ["track-001", "track-002", "track-004"]
    either = client.get("/api/v1/playlists/pl-002", params={"mytag": "warmup"}).json()
    assert [row["stable_id"] for row in either["tracks"]] == ["track-001"]


@pytest.mark.parametrize("path", ["/api/v1/tracks", "/api/v1/playlists/pl-002"])
@pytest.mark.parametrize("params", [{"mytag": "  "}, {"mytag": "peak", "mytag_mode": "xor"}])
def test_blank_mytag_or_unknown_mode_is_a_422(client: TestClient, path: str, params: dict) -> None:
    """[if] a mytag pick is blank or mytag_mode is not and/or [then] the route answers 422, [else stop]."""
    response = client.get(path, params=params)
    assert response.status_code == 422, response.text
    if params["mytag"].strip() == "":
        assert response.json()["detail"]["code"] == "mytag_invalid"


def test_sqlite_backend_matches_mytags_like_the_memory_backend(fresh_state_db: Path) -> None:  # noqa: F811
    """[if] the SQLite backend gets the same picks [then] it keeps the same rows as the in-memory one, [else stop]."""
    backend = SqliteBackend(fresh_state_db)
    both = TrackFilter(mytags=("deep-house", "smooth"), mytag_mode="and")
    assert [t.stable_id for t in backend.list_tracks(both).items] == ["sid-001"]
    missing = TrackFilter(mytags=("smooth", "absent"), mytag_mode="and")
    assert backend.list_tracks(missing).items == []
    either = TrackFilter(mytags=("smooth", "absent"), mytag_mode="or")
    assert [t.stable_id for t in backend.list_tracks(either).items] == ["sid-001"]


@pytest.fixture
def engine(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
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


def test_opendj_api_get_with_mytag_matches_http(engine: str, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] an agent runs opendj api GET /tracks?mytag=vocal&mytag=peak&mytag_mode=and [then] it gets the HTTP rows, [else stop]."""
    path = "/api/v1/tracks?mytag=vocal&mytag=peak&mytag_mode=and"
    assert main(["api", "GET", path]) == api_cli.EXIT_OK, capsys.readouterr().err
    assert _ids(json.loads(capsys.readouterr().out)["items"]) == {"track-004"}
