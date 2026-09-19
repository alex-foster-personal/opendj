"""has_rb_mapping must predict exactly which rb-meta vendor a track resolves to.

The browser uses this listing flag to skip the lazy per-row rb-meta fetch for
tracks with no rekordbox mapping. Since #505 that endpoint answers 200 for
such a track with a `local` vendor payload rather than 404ing, and every field
in it is a known constant except quality and file_exists, which the listing
row already carries -- so the fetch can only restate what the row has.

The flag is therefore only safe to gate on if it agrees with the endpoint
about WHICH vendor a track resolves to. These tests assert that equivalence in
both directions rather than merely asserting the field is present: false must
mean the `local` payload (nothing lost by skipping) and true must mean the
`rekordbox` one (never skip a row that has real meta).

Regression one-liners:
  - if has_rb_mapping is true for a track with no rekordbox vendor mapping then broken
  - if has_rb_mapping is true for a mapping whose djmdContent row is deleted then broken
  - if has_rb_mapping is true for a tombstoned mapping whose djmdContent row is still
    live then broken (resolve_content's write path already excludes it)
  - if has_rb_mapping is true for a track mapped only under a non-rekordbox vendor then broken
  - if a row reporting has_rb_mapping true does not serve vendor 'rekordbox' then broken
  - if a row reporting has_rb_mapping false does not serve vendor 'local' then broken
  - if a local rb-meta payload carries anything beyond quality/file_exists then the
    skip drops information and this whole approach is broken
  - if an unknown stable_id stops 404ing loudly then broken
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

pytestmark = [pytest.mark.requirement("CAT-05"), pytest.mark.rb_parity]

# Five tracks covering every real reason a mapping can be absent.
MAPPED = "a" * 40  # rekordbox mapping + live djmdContent row
LOCAL_ONLY = "b" * 40  # locally imported: no vendor mapping at all
DELETED_RB = "c" * 40  # mapping present, djmdContent row soft-deleted
DJAY_ONLY = "d" * 40  # mapped, but under a different vendor
TOMBSTONED_MAPPING = "e" * 40  # mapping itself soft-deleted, djmdContent row still live
VENDOR_MAPPED = "126790091"
VENDOR_DELETED = "126790092"
VENDOR_TOMBSTONED = "126790093"


def _make_master_plain_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE djmdContent (ID VARCHAR(255) PRIMARY KEY, "
            "FolderPath VARCHAR(255), ImagePath VARCHAR(255), "
            "AnalysisDataPath VARCHAR(255), Length INTEGER, "
            "Commnt VARCHAR(255), GenreID VARCHAR(255), "
            "DJPlayCount INTEGER DEFAULT 0, "
            "rb_local_deleted TINYINT(1) DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE djmdGenre (ID VARCHAR(255) PRIMARY KEY, "
            "Name VARCHAR(255), rb_local_deleted TINYINT(1) DEFAULT 0)"
        )
        # rb-meta's 200 path counts cues; empty is fine, absent is not.
        conn.execute(
            "CREATE TABLE djmdCue (ID VARCHAR(255) PRIMARY KEY, "
            "ContentID VARCHAR(255), rb_local_deleted TINYINT(1) DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO djmdContent (ID, FolderPath, Length, rb_local_deleted) "
            "VALUES (?, '/music/mapped.mp3', 300, 0)",
            (VENDOR_MAPPED,),
        )
        # Soft-deleted in rekordbox: the mapping survives in state.db, the
        # content row does not. resolve_content treats this as unmapped.
        conn.execute(
            "INSERT INTO djmdContent (ID, FolderPath, Length, rb_local_deleted) "
            "VALUES (?, '/music/deleted.mp3', 300, 1)",
            (VENDOR_DELETED,),
        )
        # The djmdContent row is LIVE here -- the tombstone is on the state.db
        # side (track_vendor_ids.deleted_at), a re-import/re-map case where the
        # old rekordbox row was never touched.
        conn.execute(
            "INSERT INTO djmdContent (ID, FolderPath, Length, rb_local_deleted) "
            "VALUES (?, '/music/tombstoned.mp3', 300, 0)",
            (VENDOR_TOMBSTONED,),
        )
        conn.commit()
    finally:
        conn.close()


def _make_state_db(path: Path) -> None:
    conn = state_db.open_rw(path)
    try:
        for sid in (MAPPED, LOCAL_ONLY, DELETED_RB, DJAY_ONLY, TOMBSTONED_MAPPING):
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, title, "
                "file_path, created_at, updated_at) VALUES "
                "(?, 'inferred', ?, ?, '2026-01-01', '2026-01-01')",
                (sid, f"track {sid[0]}", f"/music/{sid[0]}.mp3"),
            )
        conn.execute(
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) "
            "VALUES (?, 'rekordbox', ?)",
            (MAPPED, VENDOR_MAPPED),
        )
        conn.execute(
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) "
            "VALUES (?, 'rekordbox', ?)",
            (DELETED_RB, VENDOR_DELETED),
        )
        # The reason the flag is not called has_vendor_mapping: this row IS
        # vendor-mapped, just not to rekordbox, so rb-meta still 404s.
        conn.execute(
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) "
            "VALUES (?, 'djay', 'djay-0001')",
            (DJAY_ONLY,),
        )
        # The mapping row itself is tombstoned (re-import/re-map case): the
        # djmdContent row it points at is still live, so resolve_content's own
        # `deleted_at IS NULL` predicate is the only thing standing between
        # this and a false-positive has_rb_mapping (#736 review).
        conn.execute(
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id, deleted_at) "
            "VALUES (?, 'rekordbox', ?, '2026-01-01')",
            (TOMBSTONED_MAPPING, VENDOR_TOMBSTONED),
        )
        # One mapped + one unmapped member, so playlist detail proves both
        # values on the TrackRowOut wire shape.
        conn.execute(
            "INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, "
            "created_at, updated_at) VALUES ('pl-mixed', 'Mixed', 'rekordbox', "
            "'rb-pl-1', '2026-01-01', '2026-01-01')"
        )
        for position, sid in enumerate((MAPPED, LOCAL_ONLY)):
            conn.execute(
                "INSERT INTO playlist_memberships (playlist_id, stable_id, "
                "position) VALUES ('pl-mixed', ?, ?)",
                (sid, position),
            )
        conn.executemany(
            "INSERT INTO track_fields (stable_id, field_name, value_json, source, "
            "confidence, modified_at) VALUES (?, ?, ?, 'inferred', 0.7, '2026-01-01')",
            [
                (LOCAL_ONLY, "genre", '"House"'),
                (LOCAL_ONLY, "comments", '"Imported from file tags"'),
            ],
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


def _rows_by_id(client: TestClient) -> dict[str, dict]:
    response = client.get("/api/v1/tracks")
    assert response.status_code == 200, response.text
    return {row["stable_id"]: row for row in response.json()["items"]}


def test_listing_flags_only_the_live_rekordbox_mapping(client: TestClient) -> None:
    rows = _rows_by_id(client)
    assert set(rows) == {MAPPED, LOCAL_ONLY, DELETED_RB, DJAY_ONLY, TOMBSTONED_MAPPING}
    assert rows[MAPPED]["has_rb_mapping"] is True
    assert rows[LOCAL_ONLY]["has_rb_mapping"] is False
    assert rows[DELETED_RB]["has_rb_mapping"] is False
    assert rows[DJAY_ONLY]["has_rb_mapping"] is False
    assert rows[TOMBSTONED_MAPPING]["has_rb_mapping"] is False


@pytest.mark.parametrize(
    ("stable_id", "expected_flag"),
    [
        (MAPPED, True),
        (LOCAL_ONLY, False),
        (DELETED_RB, False),
        (DJAY_ONLY, False),
        (TOMBSTONED_MAPPING, False),
    ],
)
def test_flag_agrees_with_rb_meta_vendor(
    client: TestClient, stable_id: str, expected_flag: bool
) -> None:
    """The whole point of the flag: it predicts which vendor rb-meta serves.

    False must mean the `local` payload, so gating the fetch off loses
    nothing. True must mean the `rekordbox` one, so gating never hides real
    vendor meta from a row that has some.
    """
    rows = _rows_by_id(client)
    assert rows[stable_id]["has_rb_mapping"] is expected_flag

    response = client.get(f"/api/v1/tracks/{stable_id}/rb-meta")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stable_id"] == stable_id
    assert body["vendor"] == ("rekordbox" if expected_flag else "local")


def test_unmapped_rows_and_meta_read_file_tag_metadata_from_state(
    client: TestClient,
) -> None:
    """Local rows use import-time file tag values from the state layer.

    A track without a live rekordbox mapping has no djmdContent genre or
    comment to read. Its file-tag metadata belongs to state.db instead, and
    rb-meta, the hydrated playlist row, and the /tracks listing row must
    expose the same genre from it (STANDALONE-05).
    """
    body = client.get(f"/api/v1/tracks/{LOCAL_ONLY}/rb-meta").json()
    assert body["vendor"] == "local"
    assert body["vendor_id"] is None
    assert body["genre"] == "House"
    assert body["comment"] == "Imported from file tags"
    assert body["artwork_available"] is False
    assert body["analysis_available"] is False
    assert body["beatgrid_issue"] is None
    assert body["cue_count"] == 0

    detail = client.get("/api/v1/playlists/pl-mixed").json()
    row = {track["stable_id"]: track for track in detail["tracks"]}[LOCAL_ONLY]
    assert row["genre"] == "House"
    assert row["comments"] == "Imported from file tags"
    assert row["file_exists"] == body["file_exists"]
    assert row["quality"] == body["quality"]

    listing_row = _rows_by_id(client)[LOCAL_ONLY]
    assert listing_row["genre"] == "House"
    assert listing_row.get("genre_reason") is None
    assert "comments" not in listing_row


def test_unknown_stable_id_still_404s_loudly(client: TestClient) -> None:
    """The flag must not have softened the genuinely-unknown-id contract."""
    response = client.get("/api/v1/tracks/" + "f" * 40 + "/rb-meta")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "TRACK_NOT_FOUND"


def test_playlist_detail_rows_carry_the_flag_too(client: TestClient) -> None:
    """BrowserPanel gates on it for playlist panes as well as All Tracks.

    Playlist detail serves TrackRowOut, a separate model from the listing's
    TrackListItemOut, so the field has to be proven on both wire shapes.
    """
    response = client.get("/api/v1/playlists/pl-mixed")
    assert response.status_code == 200, response.text
    flags = {row["stable_id"]: row["has_rb_mapping"] for row in response.json()["tracks"]}
    assert flags == {MAPPED: True, LOCAL_ONLY: False}


@pytest.mark.parametrize(
    ("stable_id", "expected_flag"),
    [
        (MAPPED, True),
        (LOCAL_ONLY, False),
        (DELETED_RB, False),
        (DJAY_ONLY, False),
        (TOMBSTONED_MAPPING, False),
    ],
)
def test_single_track_get_carries_the_flag_too(
    client: TestClient, stable_id: str, expected_flag: bool
) -> None:
    """GET /tracks/{sid} is the ONE fetch the deck-load path makes -- the
    hot-cue SAVE inert-with-tooltip state (PARITY-TODO, issue #736) reads
    this field off the loaded deck's track, not off a listing row it may
    never have hydrated (e.g. a track loaded via search or a bare deep link).
    TrackOut, not TrackListItemOut -- a third wire shape from the other two.
    """
    response = client.get(f"/api/v1/tracks/{stable_id}")
    assert response.status_code == 200, response.text
    assert response.json()["has_rb_mapping"] is expected_flag


def test_patch_track_response_carries_the_flag_too(client: TestClient) -> None:
    """PATCH also returns TrackOut, so it must not regress to a stale/absent flag."""
    get_response = client.get(f"/api/v1/tracks/{LOCAL_ONLY}")
    etag = get_response.headers["etag"]
    response = client.patch(
        f"/api/v1/tracks/{LOCAL_ONLY}",
        json={"rating": 3},
        headers={"If-Match": etag},
    )
    assert response.status_code == 200, response.text
    assert response.json()["has_rb_mapping"] is False


@pytest.mark.parametrize(
    ("stable_id", "expected_flag"),
    [(MAPPED, True), (LOCAL_ONLY, False)],
)
def test_patch_conflict_response_carries_the_flag_too(
    client: TestClient, stable_id: str, expected_flag: bool
) -> None:
    """A 409's `current` payload is a track view too (errors.py ConflictBody),
    and the track page can "Take theirs" straight from it (#736 review). A
    stale If-Match raises ConflictError(current.to_dict(), ...) straight from
    the backend layer, bypassing _track_to_out entirely, so this must be
    proven separately from the 200 case above rather than assumed to follow.
    """
    response = client.patch(
        f"/api/v1/tracks/{stable_id}",
        json={"rating": 3},
        headers={"If-Match": '"stale-etag"'},
    )
    assert response.status_code == 409, response.text
    current = response.json()["current"]
    assert current["stable_id"] == stable_id
    assert current["has_rb_mapping"] is expected_flag
