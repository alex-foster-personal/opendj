"""LIBUX-13747: the track table's Album column, served on every hydrated row (issue #9732).

- [if] GET /playlists/{id} rows lack album [then] the Album column has nothing to show, [else stop].
- [if] GET /playlists/{id}/tracks rows lack album [then] a paged playlist shows a blank Album column, [else stop].
- [if] GET /tracks rows lack album [then] the collection shows a blank Album column, [else stop].
- [if] a track has no album [then] the row carries null, never a placeholder string, [else stop].

The hidden-columns pref (album hidden by default, PUT-able) is proven in
tests/webui/test_ui_prefs_library_hidden_columns.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.backend import InMemoryBackend
from tests.webui.app_fixtures import client_fixture  # noqa: F401  (pytest fixtures)


@pytest.fixture
def album_backend(seed_backend: InMemoryBackend) -> InMemoryBackend:
    """track-003 has an album; track-005 (same playlist) has none."""
    seed_backend.get_track("track-003").album = "Harbor EP"
    return seed_backend


def _albums(rows: list[dict]) -> dict[str, str | None]:
    return {row["stable_id"]: row["album"] for row in rows}


@pytest.mark.requirement("LIBUX-13747")
def test_playlist_detail_rows_carry_album(client: TestClient, album_backend: InMemoryBackend) -> None:
    """[if] GET /playlists/{id} is read [then] each row carries the track's album or null, [else stop]."""
    body = client.get("/api/v1/playlists/pl-001").json()
    assert _albums(body["tracks"]) == {"track-003": "Harbor EP", "track-005": None}


@pytest.mark.requirement("LIBUX-13747")
def test_playlist_track_page_rows_carry_album(client: TestClient, album_backend: InMemoryBackend) -> None:
    """[if] GET /playlists/{id}/tracks is read [then] each paged row carries album, [else stop]."""
    body = client.get("/api/v1/playlists/pl-001/tracks", params={"limit": 10}).json()
    assert _albums(body["tracks"]) == {"track-003": "Harbor EP", "track-005": None}


@pytest.mark.requirement("LIBUX-13747")
def test_collection_rows_carry_album(client: TestClient, album_backend: InMemoryBackend) -> None:
    """[if] GET /tracks is read [then] the collection row matches GET /tracks/{id} album, [else stop]."""
    items = client.get("/api/v1/tracks", params={"limit": 50}).json()["items"]
    albums = _albums(items)
    assert albums["track-003"] == "Harbor EP"
    assert albums["track-005"] is None
    assert client.get("/api/v1/tracks/track-003").json()["album"] == "Harbor EP"
