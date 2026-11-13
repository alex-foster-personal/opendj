"""Explorer source backend: folder tree and folder tracks over HTTP (issue #9747, agent parity).

[if] tracks carry stored paths under tmp_path [then] GET /library/folders and /library/folders/tracks answer them, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Track
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.webui.test_sqlite_backend import fresh_state_db  # noqa: F401  (pytest fixture)
from tests.webui.testclient_host import TEST_HOST_BASE_URL

pytestmark = pytest.mark.requirement("LIBUX-14606")


@pytest.fixture(name="music_root")
def music_root_fixture(tmp_path: Path) -> Path:
    return tmp_path / "Music"


@pytest.fixture(name="folders_client")
def folders_client_fixture(music_root: Path) -> TestClient:
    backend = InMemoryBackend()
    for sid, rel in [("t1", "House/b.mp3"), ("t2", "House/A.mp3"), ("t3", "House/Deep/c.flac"), ("t4", "Techno/d.wav")]:
        backend.seed_track(Track(stable_id=sid, title=sid, file_path=str(music_root / rel)))
    backend.seed_track(Track(stable_id="t5", title="no path", file_path=None))
    app = create_app(backend=backend, bind_host="127.0.0.1", hostname="test-host", lock_status_fn=lambda: None)
    with TestClient(app, base_url=TEST_HOST_BASE_URL) as client:
        yield client


def _node(nodes: list[dict], path: str) -> dict:
    for node in nodes:
        if node["path"] == path:
            return node
        if path.startswith(node["path"]) and (found := _node(node["children"], path)):
            return found
    return {}


def test_tree_lists_folders_with_counts(folders_client: TestClient, music_root: Path) -> None:
    """[if] GET /library/folders [then] House has 3 tracks (2 direct) and children Deep, pathless counted apart, [else stop]."""
    body = folders_client.get("/api/v1/library/folders").json()
    assert (body["track_count"], body["unlocated_count"]) == (4, 1), "if totals miss tracks or the pathless one"
    house = _node(body["roots"], str(music_root / "House"))
    assert (house["track_count"], house["direct_track_count"]) == (3, 2), "if folder counts are wrong then broken"
    assert [child["name"] for child in house["children"]] == ["Deep"]
    assert [c["name"] for c in _node(body["roots"], str(music_root))["children"]] == ["House", "Techno"]


def test_folder_tracks_direct_and_recursive(folders_client: TestClient, music_root: Path) -> None:
    """[if] GET /library/folders/tracks?path=House, then recursive=true [then] 2 rows, then 3 with Deep's, [else stop]."""
    house = str(music_root / "House")
    direct = folders_client.get("/api/v1/library/folders/tracks", params={"path": house}).json()
    assert [row["stable_id"] for row in direct["items"]] == ["t2", "t1"], "if not the folder's own tracks in order"
    assert direct["items"][0]["file_path"] == str(music_root / "House" / "A.mp3")
    nested = folders_client.get("/api/v1/library/folders/tracks", params={"path": house, "recursive": "true"}).json()
    assert (nested["track_count"], nested["recursive"]) == (3, True), "if recursive skips child folders then broken"


def test_unknown_folder_is_404(folders_client: TestClient, music_root: Path) -> None:
    """[if] a folder with no tracks is asked for [then] 404 folder_not_found, never an empty guess, [else stop]."""
    response = folders_client.get("/api/v1/library/folders/tracks", params={"path": str(music_root / "Nope")})
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "folder_not_found"


def test_sqlite_backend_reads_every_live_path_in_one_query(fresh_state_db: Path) -> None:  # noqa: F811
    """[if] SqliteBackend.list_file_paths [then] it matches each live track's stored file_path, [else stop]."""
    paths = SqliteBackend(fresh_state_db).list_file_paths()
    assert paths == {"sid-001": "/music/midnight.mp3", "sid-002": "/music/oxide.mp3", "sid-003": "/music/gulf.flac"}
