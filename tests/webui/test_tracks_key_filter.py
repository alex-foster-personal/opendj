"""LIBUX-13928: Key column funnel picks on the tracks list and playlist detail (issue #9739).

- [if] GET /tracks?key=8A&key=7A keeps a row in another key [then] the Key funnel lies, [else stop].
- [if] a key in another notation (Am, 1m) does not match its Camelot rows [then] the API is notation-bound, [else stop].
- [if] an unknown key (13A, zz) is accepted [then] a typo silently empties the list, [else stop].
- [if] opendj api GET with keys differs from HTTP [then] agent parity is broken, [else stop].
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

pytestmark = pytest.mark.requirement("LIBUX-13928")

# seed_backend keys: track-001 8A, track-002 7A, track-003 5A, track-004 11A, track-005 3B.


def _ids(rows: list[dict]) -> set[str]:
    return {row["stable_id"] for row in rows}


def test_tracks_list_keeps_rows_in_any_picked_key(client: TestClient) -> None:
    """[if] GET /tracks gets key=8A&key=7A [then] only the 8A and 7A rows come back, [else stop]."""
    response = client.get("/api/v1/tracks", params=[("key", "8A"), ("key", "7A")])
    assert response.status_code == 200, response.text
    assert _ids(response.json()["items"]) == {"track-001", "track-002"}
    unfiltered = client.get("/api/v1/tracks").json()["items"]
    assert len(unfiltered) == 5


@pytest.mark.parametrize(("raw", "expected"), [("Am", {"track-001"}), ("1m", {"track-001"}), ("C", set())])
def test_any_notation_maps_to_its_camelot_key(client: TestClient, raw: str, expected: set[str]) -> None:
    """[if] key=Am, key=1m or key=C is sent [then] it matches the rows of its Camelot key, [else stop]."""
    response = client.get("/api/v1/tracks", params={"key": raw})
    assert response.status_code == 200, response.text
    assert _ids(response.json()["items"]) == expected


def test_playlist_detail_filters_rows_by_key_and_keeps_membership(client: TestClient) -> None:
    """[if] GET /playlists/{id} gets key=11A [then] tracks narrow to 11A and items stay full, [else stop]."""
    body = client.get("/api/v1/playlists/pl-002", params={"key": "11A"}).json()
    assert [row["stable_id"] for row in body["tracks"]] == ["track-004"]
    assert body["items"] == ["track-001", "track-002", "track-004"]


@pytest.mark.parametrize("path", ["/api/v1/tracks", "/api/v1/playlists/pl-002"])
@pytest.mark.parametrize("bad", ["13A", "zz", "All", ""])
def test_unknown_key_is_a_422(client: TestClient, path: str, bad: str) -> None:
    """[if] key is 13A, zz, the All sentinel or blank [then] the route answers 422 key_invalid, [else stop]."""
    response = client.get(path, params=[("key", "8A"), ("key", bad)])
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "key_invalid"


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


def test_opendj_api_get_with_keys_matches_http(engine: str, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] an agent runs opendj api GET /tracks?key=8A&key=7A [then] it gets the HTTP rows, [else stop]."""
    assert main(["api", "GET", "/api/v1/tracks?key=8A&key=7A"]) == api_cli.EXIT_OK, capsys.readouterr().err
    assert _ids(json.loads(capsys.readouterr().out)["items"]) == {"track-001", "track-002"}
