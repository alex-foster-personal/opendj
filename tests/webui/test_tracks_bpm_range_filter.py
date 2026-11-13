"""LIBUX-13909: BPM column funnel range on the tracks list and playlist detail (issue #9738).

- [if] GET /tracks?bpm_min=&bpm_max= keeps a row outside the inclusive range [then] the funnel lies, [else stop].
- [if] playlist detail ignores bpm_min/bpm_max or drops membership items [then] agents cannot filter a list, [else stop].
- [if] a bpm_min above bpm_max or a negative bound is accepted [then] an empty list hides a typo, [else stop].
- [if] opendj api GET with a BPM range differs from HTTP [then] agent parity is broken, [else stop].
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
from tests.waits import start_uvicorn_in_thread
from tests.webui.app_fixtures import client_fixture  # noqa: F401
from tests.webui.conftest import seed_backend  # noqa: F401

pytestmark = pytest.mark.requirement("LIBUX-13909")

# seed_backend BPMs: track-001 124, track-002 128, track-003 118, track-004 140, track-005 92.


def _ids(rows: list[dict]) -> set[str]:
    return {row["stable_id"] for row in rows}


def test_tracks_list_keeps_only_the_inclusive_bpm_range(client: TestClient) -> None:
    """[if] GET /tracks gets bpm_min=118&bpm_max=128 [then] exactly 118, 124 and 128 BPM rows come back, [else stop]."""
    both = client.get("/api/v1/tracks", params={"bpm_min": 118, "bpm_max": 128})
    assert both.status_code == 200, both.text
    assert _ids(both.json()["items"]) == {"track-001", "track-002", "track-003"}
    floor = client.get("/api/v1/tracks", params={"bpm_min": 128})
    assert _ids(floor.json()["items"]) == {"track-002", "track-004"}
    ceiling = client.get("/api/v1/tracks", params={"bpm_max": 92})
    assert _ids(ceiling.json()["items"]) == {"track-005"}


def test_playlist_detail_filters_rows_and_keeps_membership(client: TestClient) -> None:
    """[if] GET /playlists/{id} gets a BPM range [then] tracks narrow and items stay full, [else stop]."""
    body = client.get("/api/v1/playlists/pl-002", params={"bpm_min": 120, "bpm_max": 130}).json()
    assert [row["stable_id"] for row in body["tracks"]] == ["track-001", "track-002"]
    assert body["items"] == ["track-001", "track-002", "track-004"]
    unfiltered = client.get("/api/v1/playlists/pl-002").json()
    assert len(unfiltered["tracks"]) == 3


@pytest.mark.parametrize("path", ["/api/v1/tracks", "/api/v1/playlists/pl-002"])
@pytest.mark.parametrize("params", [{"bpm_min": 130, "bpm_max": 120}, {"bpm_min": -1}, {"bpm_max": "fast"}])
def test_bad_bpm_range_is_a_422(client: TestClient, path: str, params: dict) -> None:
    """[if] min is above max, a bound is negative or not a number [then] the route answers 422, [else stop]."""
    response = client.get(path, params=params)
    assert response.status_code == 422, response.text


@pytest.fixture
def engine(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:  # noqa: F811
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


def test_opendj_api_get_with_a_bpm_range_matches_http(
    engine: str, client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] an agent runs opendj api GET /tracks with a BPM range [then] it gets the HTTP rows, [else stop]."""
    path = "/api/v1/playlists/pl-002?bpm_min=120&bpm_max=130"
    assert main(["api", "GET", path]) == api_cli.EXIT_OK, capsys.readouterr().err
    body = json.loads(capsys.readouterr().out)
    assert [row["stable_id"] for row in body["tracks"]] == ["track-001", "track-002"]
    assert main(["api", "GET", "/api/v1/tracks?bpm_min=118&bpm_max=128"]) == api_cli.EXIT_OK
    assert _ids(json.loads(capsys.readouterr().out)["items"]) == {"track-001", "track-002", "track-003"}
