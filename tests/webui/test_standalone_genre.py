"""STANDALONE-05: genre works without rekordbox and never fails silently.

- [if] a folder GENRE tag and no rekordbox [then] the wheel files it in a family, [else stop].
- [if] the optional tags extra is not installed [then] the genre column names it, [else stop].
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.library_wheel.query import query_library_wheel
from apps.shared import paths as shared_paths
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.rb_vendor_pkg.track_rows import GENRE_REASON_NO_FILE_TAG
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.webui.library_wheel_fixtures import _make_master_db, _make_state_db

pytest_plugins = ("tests.webui.library_wheel_fixtures",)

pytestmark = pytest.mark.requirement("STANDALONE-05")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_ONLY_SID = "t-local-house"
MAPPED_SID = "t-techno-1"
UNTAGGED_SID = "t-no-rb-1"


def test_state_only_query_places_tagged_track_in_genre_family(
    state_only_wheel_db: Path,
) -> None:
    """[if] folder import has GENRE and no rekordbox [then] wheel family, [else stop]."""
    master_db = state_only_wheel_db.parent / "absent-master.plain.db"
    result = query_library_wheel(state_only_wheel_db, master_db, axis="play_count")

    assert result["total_tracks"] == 1
    assert result["unclassified_track_count"] == 0
    assert result["families"][0]["name"] == "house"
    track = result["families"][0]["genres"][0]["tracks"][0]
    assert track["stable_id"] == STATE_ONLY_SID
    assert track["genre"] == "House"
    assert "DJPlayCount" not in track["axis_title"]


def test_state_only_query_does_not_require_master_plain_db(
    state_only_wheel_db: Path,
) -> None:
    """[if] library has no rekordbox mappings [then] master.plain.db is not opened, [else stop]."""
    missing_master = state_only_wheel_db.parent / "does-not-exist.plain.db"
    assert not missing_master.is_file()
    result = query_library_wheel(state_only_wheel_db, missing_master, axis="genre")
    assert result["unclassified_track_count"] == 0


def test_mapped_track_still_uses_rekordbox_genre(wheel_dbs: tuple[Path, Path]) -> None:
    """[if] a rekordbox mapping exists [then] rekordbox genre wins, [else stop]."""
    state_db, master_db = wheel_dbs
    result = query_library_wheel(state_db, master_db, axis="play_count")
    track = _find_track(result, MAPPED_SID)
    assert track["genre"] == "Peak Time Techno"
    assert "rekordbox" in track["axis_title"].lower()


def test_mapped_track_ignores_conflicting_local_genre(tmp_path: Path) -> None:
    """[if] a mapped track also has a local genre field [then] rekordbox genre wins, [else stop]."""
    state_db = tmp_path / "state.db"
    master_db = tmp_path / "master.plain.db"
    _make_state_db(
        state_db,
        tracks=[
            {
                "stable_id": "t-mapped-conflict",
                "title": "Mapped Conflict",
                "artists": ["Artist F"],
                "file_path": "/music/mapped-conflict.mp3",
            },
        ],
        memberships=[],
        playlists=[],
        vendor_ids={"t-mapped-conflict": "v-conflict"},
        track_fields=[
            {
                "stable_id": "t-mapped-conflict",
                "field_name": "genre",
                "value_json": '"House"',
            },
        ],
    )
    _make_master_db(
        master_db,
        content=[{"vendor_id": "v-conflict", "genre_id": "g-techno", "play_count": 3}],
    )
    result = query_library_wheel(state_db, master_db, axis="play_count")
    assert result["unclassified_track_count"] == 0
    track = _find_track(result, "t-mapped-conflict")
    assert track["genre"] == "Peak Time Techno"
    assert result["families"][0]["name"] == "techno"
    assert track["axis_value"] == 3
    assert "rekordbox" in track["axis_title"].lower()


def test_mapped_track_without_live_rekordbox_content_uses_local_genre(tmp_path: Path) -> None:
    """[if] vendor id has no live rekordbox content [then] local genre is used, [else stop]."""
    state_db = tmp_path / "state.db"
    master_db = tmp_path / "master.plain.db"
    _make_state_db(
        state_db,
        tracks=[
            {
                "stable_id": "t-stale-mapping",
                "title": "Stale Mapping",
                "artists": ["Artist G"],
                "file_path": "/music/stale-mapping.mp3",
            },
        ],
        memberships=[],
        playlists=[],
        vendor_ids={"t-stale-mapping": "v-gone"},
        track_fields=[
            {"stable_id": "t-stale-mapping", "field_name": "genre", "value_json": '"House"'},
        ],
    )
    _make_master_db(master_db, content=[])
    result = query_library_wheel(state_db, master_db, axis="play_count")
    assert result["unclassified_track_count"] == 0
    track = _find_track(result, "t-stale-mapping")
    assert track["genre"] == "House"
    assert result["families"][0]["name"] == "house"
    assert "rekordbox" not in track["axis_title"].lower().replace("no rekordbox", "")


def test_tombstoned_state_genre_counts_as_unclassified(tmp_path: Path) -> None:
    """[if] local genre field is tombstoned [then] wheel unclassified, [else stop]."""
    state_db = tmp_path / "state.db"
    master_db = tmp_path / "master.plain.db"
    _make_state_db(
        state_db,
        tracks=[
            {
                "stable_id": "t-cleared-genre",
                "title": "Cleared Genre",
                "artists": ["Local Artist"],
                "file_path": "/music/cleared.mp3",
            },
        ],
        memberships=[],
        playlists=[],
        vendor_ids={},
        track_fields=[
            {
                "stable_id": "t-cleared-genre",
                "field_name": "genre",
                "value_json": '"House"',
                "deleted_at": "2026-01-02T00:00:00Z",
            },
        ],
    )
    result = query_library_wheel(state_db, master_db, axis="genre")
    assert result["total_tracks"] == 1
    assert result["unclassified_track_count"] == 1
    with pytest.raises(AssertionError):
        _find_track(result, "t-cleared-genre")


def test_unmapped_track_without_genre_stays_unclassified(wheel_dbs: tuple[Path, Path]) -> None:
    """[if] unmapped track has no genre tag [then] unclassified count rises, [else stop]."""
    state_db, master_db = wheel_dbs
    result = query_library_wheel(state_db, master_db, axis="genre")
    assert result["unclassified_track_count"] >= 1
    with pytest.raises(AssertionError):
        _find_track(result, UNTAGGED_SID)


@pytest.fixture
def state_only_client(
    state_only_wheel_db: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    master_db = state_only_wheel_db.parent / "absent-master.plain.db"
    monkeypatch.setattr(shared_paths, "STATE_DB", state_only_wheel_db)
    monkeypatch.setattr(shared_paths, "REKORDBOX_PLAIN_DB", master_db)
    monkeypatch.setattr(rb_config, "STATE_DB", state_only_wheel_db)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master_db)
    app = create_app(
        backend=SqliteBackend(state_only_wheel_db),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_only_wheel_db),
        mount_frontend=False,
    )
    with TestClient(app) as client:
        yield client


def test_wheel_route_serves_state_only_genre_family(
    state_only_client: TestClient,
) -> None:
    """[if] HTTP wheel on state-only library [then] house family, zero unclassified, [else stop]."""
    resp = state_only_client.get("/api/v1/library/wheel", params={"axis": "genre"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unclassified_track_count"] == 0
    assert body["families"][0]["name"] == "house"


def test_tracks_listing_carries_state_only_genre(state_only_client: TestClient) -> None:
    """[if] state-only tagged row on /tracks [then] genre is House with no reason, [else stop]."""
    resp = state_only_client.get("/api/v1/tracks")
    assert resp.status_code == 200, resp.text
    row = {item["stable_id"]: item for item in resp.json()["items"]}[STATE_ONLY_SID]
    assert row["genre"] == "House"
    assert row.get("genre_reason") is None


@pytest.fixture
def tombstoned_genre_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    state_path = tmp_path / "state.db"
    master_path = tmp_path / "master.plain.db"
    _make_state_db(
        state_path,
        tracks=[
            {
                "stable_id": "t-tombstoned",
                "title": "Cleared Genre",
                "artists": [],
                "file_path": "/music/cleared.mp3",
            },
        ],
        memberships=[],
        playlists=[],
        vendor_ids={},
        track_fields=[
            {
                "stable_id": "t-tombstoned",
                "field_name": "genre",
                "value_json": '"House"',
                "deleted_at": "2026-01-02T00:00:00Z",
            },
        ],
    )
    monkeypatch.setattr(shared_paths, "STATE_DB", state_path)
    monkeypatch.setattr(shared_paths, "REKORDBOX_PLAIN_DB", master_path)
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master_path)
    app = create_app(
        backend=SqliteBackend(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_path),
        mount_frontend=False,
    )
    with TestClient(app) as client:
        yield client


def test_tracks_listing_hides_tombstoned_genre(tombstoned_genre_client: TestClient) -> None:
    """[if] the genre field is tombstoned [then] /tracks has no genre and a reason, [else stop]."""
    resp = tombstoned_genre_client.get("/api/v1/tracks")
    assert resp.status_code == 200, resp.text
    row = resp.json()["items"][0]
    assert row["genre"] is None
    assert row["genre_reason"] == GENRE_REASON_NO_FILE_TAG


@pytest.fixture
def untagged_state_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    state_path = tmp_path / "state.db"
    master_path = tmp_path / "master.plain.db"
    _make_state_db(
        state_path,
        tracks=[
            {
                "stable_id": "t-untagged",
                "title": "No Tags",
                "artists": [],
                "file_path": "/music/untagged.mp3",
            },
        ],
        memberships=[],
        playlists=[],
        vendor_ids={},
    )
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master_path)
    app = create_app(
        backend=SqliteBackend(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_path),
        mount_frontend=False,
    )
    with TestClient(app) as client:
        yield client


def test_tracks_listing_names_no_file_tag_reason_for_an_untagged_file(
    untagged_state_client: TestClient,
) -> None:
    """[if] a file has no genre tag [then] the no-file-tag reason is named, [else stop]."""
    resp = untagged_state_client.get("/api/v1/tracks")
    assert resp.status_code == 200, resp.text
    row = resp.json()["items"][0]
    assert row["genre"] is None
    assert row["genre_reason"] == GENRE_REASON_NO_FILE_TAG


def test_tracks_listing_genre_reason_needs_no_mutagen(
    tmp_path: Path,
) -> None:
    """[if] mutagen is unimportable [then] genre_reason is still no-file-tag, [else stop]."""
    state_path = tmp_path / "state.db"
    conn = state_db.open_rw(state_path)
    try:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, file_path, "
            "created_at, updated_at) VALUES (?, 'inferred', ?, ?, '2026-01-01', '2026-01-01')",
            ("a" * 40, "Untagged", str(tmp_path / "x.mp3")),
        )
        conn.commit()
    finally:
        conn.close()

    probe = textwrap.dedent(
        f"""
        import sys
        sys.modules["mutagen"] = None

        from pathlib import Path

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from apps.adapters.rekordbox import config as rb_config
        from apps.webui.server.app import create_app
        from apps.webui.server.sqlite_backend import SqliteBackend

        state_path = Path({str(state_path)!r})
        rb_config.STATE_DB = state_path
        rb_config.MASTER_PLAIN_DB = state_path.parent / "absent.db"

        app = create_app(
            backend=SqliteBackend(state_path),
            bind_host="127.0.0.1",
            hostname="test-host",
            state_db_path=str(state_path),
            mount_frontend=False,
        )
        with TestClient(app, base_url="http://test-host") as client:
            resp = client.get("/api/v1/tracks")

        assert resp.status_code == 200, resp.text
        row = resp.json()["items"][0]
        assert row["genre"] is None
        print("GENRE_REASON=" + str(row["genre_reason"]))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-"],
        input=probe,
        text=True,
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    reason_lines = [
        line for line in completed.stdout.splitlines() if line.startswith("GENRE_REASON=")
    ]
    assert len(reason_lines) == 1, completed.stdout
    reason = reason_lines[0].removeprefix("GENRE_REASON=")
    assert reason == GENRE_REASON_NO_FILE_TAG


def _find_track(result: dict, stable_id: str) -> dict:
    for family in result["families"]:
        for genre in family["genres"]:
            for track in genre["tracks"]:
                if track["stable_id"] == stable_id:
                    return track
    raise AssertionError(f"{stable_id} not found in result")
