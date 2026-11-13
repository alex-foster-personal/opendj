"""LIBUX-13890: track colour label: PATCH field, rekordbox fallback and the Colour column rows (issue #9737).

- [if] PATCH /tracks/{sid} refuses color_id 1..8 or null under If-Match [then] the row menu cannot set it, [else stop].
- [if] PATCH accepts 0, 9, a string or a bool for color_id [then] junk reaches the Colour column, [else stop].
- [if] an imported rekordbox track hides its ColorID before any edit [then] the column is blank, [else stop].
- [if] an Open DJ set or clear is overridden by rekordbox on the next read [then] the edit is lost, [else stop].

The Colour submenu and the column sort are proven in
apps/webui/frontend/tests/unit/track-colour-label.test.mjs.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("LIBUX-13890")

FROM_REKORDBOX = "a" * 40  # rekordbox ColorID 3 (Orange)
LOCAL_ONLY = "b" * 40  # no rekordbox row, never coloured
VENDOR_ID = "126790091"


def _make_master_plain_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE djmdContent (ID VARCHAR(255) PRIMARY KEY, FolderPath VARCHAR(255), "
            "ImagePath VARCHAR(255), AnalysisDataPath VARCHAR(255), Length INTEGER, "
            "Commnt VARCHAR(255), GenreID VARCHAR(255), DJPlayCount INTEGER DEFAULT 0, "
            "ColorID VARCHAR(255), rb_local_deleted TINYINT(1) DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE djmdGenre (ID VARCHAR(255) PRIMARY KEY, Name VARCHAR(255), "
            "rb_local_deleted TINYINT(1) DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO djmdContent (ID, FolderPath, Length, ColorID) VALUES (?, '/music/a.mp3', 300, '3')",
            (VENDOR_ID,),
        )
        conn.commit()
    finally:
        conn.close()


def _make_state_db(path: Path) -> None:
    conn = state_db.open_rw(path)
    try:
        for sid, title in ((FROM_REKORDBOX, "Harbor"), (LOCAL_ONLY, "Lumen")):
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, title, file_path, created_at, updated_at) "
                "VALUES (?, 'inferred', ?, ?, '2026-11-13T00:00:00+00:00', '2026-11-13T00:00:00+00:00')",
                (sid, title, f"/music/{sid[0]}.mp3"),
            )
        conn.execute(
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES (?, 'rekordbox', ?)",
            (FROM_REKORDBOX, VENDOR_ID),
        )
        conn.execute(
            "INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, created_at, updated_at) "
            "VALUES ('pl-col', 'Colours', 'rekordbox', 'rb-pl-1', '2026-11-13', '2026-11-13')"
        )
        for position, sid in enumerate((FROM_REKORDBOX, LOCAL_ONLY)):
            conn.execute(
                "INSERT INTO playlist_memberships (playlist_id, stable_id, position) VALUES ('pl-col', ?, ?)",
                (sid, position),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    master_path = tmp_path / "master.plain.db"
    state_path = tmp_path / "state.db"
    _make_master_plain_db(master_path)
    _make_state_db(state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master_path)
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    app = create_app(
        backend=SqliteBackend(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_path),
        mount_frontend=False,
    )
    with TestClient(app) as test_client:
        yield test_client


def _patch_colour(client: TestClient, sid: str, body: dict) -> object:
    etag = client.get(f"/api/v1/tracks/{sid}").headers["ETag"]
    return client.patch(f"/api/v1/tracks/{sid}", json=body, headers={"If-Match": etag})


def _colours(client: TestClient) -> tuple[dict[str, int | None], dict[str, int | None]]:
    playlist = client.get("/api/v1/playlists/pl-col")
    assert playlist.status_code == 200, playlist.text
    listing = client.get("/api/v1/tracks", params={"limit": 50, "include_non_music": "true"})
    assert listing.status_code == 200, listing.text
    return (
        {row["stable_id"]: row["color_id"] for row in playlist.json()["tracks"]},
        {row["stable_id"]: row["color_id"] for row in listing.json()["items"]},
    )


@pytest.mark.parametrize("color_id", [1, 8, None])
def test_patch_sets_or_clears_the_colour_under_if_match(client: TestClient, color_id: int | None) -> None:
    """[if] PATCH sends color_id 1, 8 or null with If-Match [then] it saves and echoes it, [else stop]."""
    response = _patch_colour(client, LOCAL_ONLY, {"color_id": color_id})
    assert response.status_code == 200, response.text
    assert response.json()["color_id"] == color_id
    assert client.patch(f"/api/v1/tracks/{LOCAL_ONLY}", json={"color_id": 2}).status_code == 428


@pytest.mark.parametrize("bad", [0, 9, -1, "3", True, 2.5])
def test_patch_refuses_any_other_colour_value(client: TestClient, bad: object) -> None:
    """[if] PATCH sends color_id 0, 9, -1, a string, a bool or a float [then] it is a 422, [else stop]."""
    response = _patch_colour(client, LOCAL_ONLY, {"color_id": bad})
    assert response.status_code == 422, response.text


def test_rekordbox_colour_shows_until_an_open_dj_edit_wins(client: TestClient) -> None:
    """[if] a rekordbox ColorID is read, then set, then cleared [then] rows show 3, then 6, then null, [else stop]."""
    assert _colours(client) == ({FROM_REKORDBOX: 3, LOCAL_ONLY: None},) * 2

    assert _patch_colour(client, FROM_REKORDBOX, {"color_id": 6}).status_code == 200
    assert _colours(client) == ({FROM_REKORDBOX: 6, LOCAL_ONLY: None},) * 2

    # A clear is an edit too: the rekordbox ColorID must not come back.
    assert _patch_colour(client, FROM_REKORDBOX, {"color_id": None}).status_code == 200
    assert _colours(client) == ({FROM_REKORDBOX: None, LOCAL_ONLY: None},) * 2


def test_other_fields_leave_the_colour_untouched(client: TestClient) -> None:
    """[if] a PATCH without color_id changes the colour [then] a rating edit wipes labels, [else stop]."""
    assert _patch_colour(client, LOCAL_ONLY, {"color_id": 5}).status_code == 200
    assert _patch_colour(client, LOCAL_ONLY, {"rating": 4}).json()["color_id"] == 5
