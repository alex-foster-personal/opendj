"""LIBUX-13958: Genre and Rating column funnels on the tracks list and playlist detail (issue #9740).

- [if] GET /tracks?genre=House&genre=Techno keeps a row in another genre [then] the Genre funnel lies, [else stop].
- [if] a genre pick matches only one letter case [then] the API and the UI disagree on case, [else stop].
- [if] GET /tracks?rating_min=4 keeps an unrated or lower-rated row [then] the Rating funnel lies, [else stop].
- [if] rating_min=0 or 6 or a blank genre is accepted [then] a typo silently changes the list, [else stop].
- [if] opendj api GET with genre and rating_min differs from HTTP [then] agent parity is broken, [else stop].
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
from apps.webui.server.backend import InMemoryBackend, Provenance
from tests.waits import start_uvicorn_in_thread
from tests.webui.app_fixtures import client_fixture  # noqa: F401
from tests.webui.conftest import seed_backend  # noqa: F401

pytestmark = pytest.mark.requirement("LIBUX-13958")

# seed_backend ratings: track-001 4, track-002 3, track-003 5, track-004 2, track-005 4.
GENRES = {"track-001": "House", "track-002": "Techno", "track-003": "house", "track-004": "Trance"}


@pytest.fixture(autouse=True)
def _genres(seed_backend: InMemoryBackend) -> None:  # noqa: F811
    """Give four seeded tracks an imported genre tag; track-005 stays without one."""
    for sid, genre in GENRES.items():
        seed_backend._tracks[sid].provenance["genre"] = Provenance(
            value=genre, source="rekordbox", confidence=1.0, modified_at="2026-04-17T10:00:00.000000Z", status="ok"
        )


def _ids(rows: list[dict]) -> set[str]:
    return {row["stable_id"] for row in rows}


def test_tracks_list_keeps_rows_in_any_picked_genre(client: TestClient) -> None:
    """[if] GET /tracks gets genre=house&genre=TECHNO [then] only the House, house and Techno rows come back, [else stop]."""
    response = client.get("/api/v1/tracks", params=[("genre", "house"), ("genre", "TECHNO")])
    assert response.status_code == 200, response.text
    assert _ids(response.json()["items"]) == {"track-001", "track-002", "track-003"}
    assert len(client.get("/api/v1/tracks").json()["items"]) == 5


def test_tracks_list_rating_min_hides_lower_and_unrated(client: TestClient) -> None:
    """[if] GET /tracks gets rating_min=4 [then] only rows with 4 or 5 stars come back, [else stop]."""
    response = client.get("/api/v1/tracks", params={"rating_min": 4})
    assert response.status_code == 200, response.text
    assert _ids(response.json()["items"]) == {"track-001", "track-003", "track-005"}


def test_genre_and_rating_combine(client: TestClient) -> None:
    """[if] GET /tracks gets genre=House and rating_min=5 [then] only the 5-star house row stays, [else stop]."""
    response = client.get("/api/v1/tracks", params={"genre": "House", "rating_min": 5})
    assert _ids(response.json()["items"]) == {"track-003"}


def test_playlist_detail_filters_rows_by_genre_and_rating_and_keeps_membership(client: TestClient) -> None:
    """[if] GET /playlists/{id} gets genre and rating_min [then] tracks narrow and items stay full, [else stop]."""
    by_genre = client.get("/api/v1/playlists/pl-002", params=[("genre", "techno"), ("genre", "trance")]).json()
    assert [row["stable_id"] for row in by_genre["tracks"]] == ["track-002", "track-004"]
    assert by_genre["items"] == ["track-001", "track-002", "track-004"]
    by_rating = client.get("/api/v1/playlists/pl-002", params={"rating_min": 3}).json()
    assert [row["stable_id"] for row in by_rating["tracks"]] == ["track-001", "track-002"]


@pytest.mark.parametrize("path", ["/api/v1/tracks", "/api/v1/playlists/pl-002"])
@pytest.mark.parametrize("params", [{"rating_min": 0}, {"rating_min": 6}, {"rating_min": "x"}, {"genre": "  "}])
def test_out_of_range_rating_or_blank_genre_is_a_422(client: TestClient, path: str, params: dict) -> None:
    """[if] rating_min is outside 1 to 5 or a genre pick is blank [then] the route answers 422, [else stop]."""
    response = client.get(path, params=params)
    assert response.status_code == 422, response.text
    if "genre" in params:
        assert response.json()["detail"]["code"] == "genre_invalid"


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


def test_opendj_api_get_with_genre_and_rating_matches_http(engine: str, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] an agent runs opendj api GET /tracks?genre=house&rating_min=4 [then] it gets the HTTP rows, [else stop]."""
    assert main(["api", "GET", "/api/v1/tracks?genre=house&rating_min=4"]) == api_cli.EXIT_OK, capsys.readouterr().err
    assert _ids(json.loads(capsys.readouterr().out)["items"]) == {"track-001", "track-003"}
