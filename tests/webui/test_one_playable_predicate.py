"""One playable predicate for the playlist tree, rows and deck load (issue #3934).

The tree's ``available_count``, the row wire's ``file_availability`` and the
deck-load audio route must agree on which tracks play, because the tree's
count decides whether hide-broken shows a playlist at all.

[if] a track's only copy is at its rekordbox FolderPath [then] the tree
     counts it, its row is present AND the audio route serves it [else stop].
[if] a playlist's members only exist at their FolderPaths [then] its
     available_count equals its track_count and every member serves, so
     hide-broken neither hides it nor shows a playlist that cannot play [else stop].
[if] the tree counts a track the audio route refuses, or the reverse for a
     checked track [then] the parity test goes red [else stop].

[if] tree, rows and deck load disagree on a playable track [then] fail, [else stop].
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.shared.state import sync_stamp
from apps.webui.server import path_availability_refresh
from apps.webui.server.app import create_app
from apps.webui.server.rb_vendor_pkg import path_index, track_rows
from apps.webui.server.sqlite_backend import SqliteBackend

from .conftest import TEST_HOST_BASE_URL, _stub_rb_vendor

pytestmark = pytest.mark.requirement("LIBM-169")

ISO = "2026-10-05T10:00:00.000000Z"
REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_AUDIO = (
    REPO_ROOT / "tests" / "fixtures" / "conformance" / "03-8-hot-cues" / "audio" / "cues.mp3"
)
UNMOUNTED_VOLUME = "/Volumes/opendj-test-never-mounted-3934"


@dataclass(frozen=True)
class Case:
    """One fixture track and what each surface must say about it."""

    sid: str
    vendor_id: str | None
    plays: bool
    checked: bool = True


PRESENT_AT_FILE_PATH = Case("a" * 40, None, plays=True)
PRESENT_ONLY_AT_FOLDER_PATH = Case("b" * 40, "9001", plays=True)
ABSENT = Case("c" * 40, None, plays=False)
STREAMING = Case("d" * 40, "9002", plays=False)
UNCHECKED = Case("e" * 40, None, plays=True, checked=False)
AWAITING_VOLUME = Case("f" * 40, "9003", plays=False)
PARITY_CASES = (
    PRESENT_AT_FILE_PATH,
    PRESENT_ONLY_AT_FOLDER_PATH,
    ABSENT,
    STREAMING,
    UNCHECKED,
    AWAITING_VOLUME,
)
FOLDER_ONLY_SIDS = [f"{i:x}" * 40 for i in range(1, 4)]
BROKEN_SIDS = [f"0{i}" * 20 for i in range(1, 4)]


# ----------------------------------------------------------------------------
# Fixture: a state.db and a master.plain.db shaped like the Air preview data,
# where track_availability recorded the other machine's tracks.file_path as
# absent and rekordbox's FolderPath is on this disk.


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_audio(dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REAL_AUDIO, dest)
    return dest


def _create_master(master_path: Path) -> sqlite3.Connection:
    master = sqlite3.connect(master_path)
    master.executescript(
        "CREATE TABLE djmdContent (ID TEXT, FolderPath TEXT, ImagePath TEXT, "
        "AnalysisDataPath TEXT, Length INTEGER, Commnt TEXT, GenreID TEXT, "
        "DJPlayCount INTEGER DEFAULT 0, rb_local_deleted INTEGER DEFAULT 0);"
        "CREATE TABLE djmdGenre (ID TEXT, Name TEXT, rb_local_deleted INTEGER DEFAULT 0);"
    )
    return master


def _insert_track(
    conn: sqlite3.Connection,
    sid: str,
    file_path: str | None,
    *,
    content_hash: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, duration_ms, "
        "file_path, content_hash, created_at, updated_at) "
        "VALUES (?, 'fingerprint', ?, ?, 180000, ?, ?, ?, ?)",
        (sid, f"Track {sid[:4]}", json.dumps(["Artist"]), file_path, content_hash, ISO, ISO),
    )


def _map_to_folder_path(
    conn: sqlite3.Connection,
    master: sqlite3.Connection,
    sid: str,
    vendor_id: str,
    folder_path: str,
) -> None:
    conn.execute(
        "INSERT INTO track_vendor_ids(stable_id, vendor, vendor_id) "
        "VALUES (?, 'rekordbox', ?)",
        (sid, vendor_id),
    )
    master.execute(
        "INSERT INTO djmdContent(ID, FolderPath) VALUES (?, ?)", (vendor_id, folder_path)
    )


def _record_checked(conn: sqlite3.Connection, sid: str, state: str, path: str | None) -> None:
    conn.execute(
        "INSERT INTO track_availability(stable_id, state, checked_path, checked_at) "
        "VALUES (?, ?, ?, ?)",
        (sid, state, path, ISO),
    )


def _add_playlist(conn: sqlite3.Connection, playlist_id: str, sids: list[str]) -> None:
    conn.execute(
        "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, created_at, "
        "updated_at) VALUES (?, ?, 'rekordbox', ?, ?, ?)",
        (playlist_id, playlist_id, f"RB-{playlist_id}", ISO, ISO),
    )
    for pos, sid in enumerate(sids):
        conn.execute(
            "INSERT INTO playlist_memberships(playlist_id, stable_id, position) "
            "VALUES (?, ?, ?)",
            (playlist_id, sid, pos),
        )


def _seed_folder_only_track(
    conn: sqlite3.Connection,
    master: sqlite3.Connection,
    root: Path,
    sid: str,
    vendor_id: str,
) -> str:
    """The live shape: tracks.file_path is another machine's path, recorded
    absent; rekordbox's FolderPath is on this disk."""
    folder = _copy_audio(root / "rekordbox" / f"{sid[:8]}.mp3")
    other_machine_path = f"/contents_815473895/{sid[:8]}.mp3"
    _insert_track(conn, sid, other_machine_path)
    _map_to_folder_path(conn, master, sid, vendor_id, str(folder))
    _record_checked(conn, sid, "absent", other_machine_path)
    return str(folder)


def _seed(state_path: Path, data_dir: Path) -> None:
    root = data_dir / "music"
    conn = state_db.open_rw(state_path)
    master = _create_master(rb_config.MASTER_PLAIN_DB)
    try:
        state_schema.apply_migrations(conn)
        machine_id = sync_stamp.ensure_local_machine(conn)
        # A configured machine, as on the Air preview: before the fix the route
        # answered CLOUD_ASSET_UNAVAILABLE here, never trying the FolderPath.
        conn.execute(
            "INSERT INTO sync_policies(machine_id, asset_kind, mode, cache_budget_mb, "
            "updated_at) VALUES (?, 'audio', 'pinned', 100, ?)",
            (machine_id, ISO),
        )
        indexed: list[str] = []

        present = _copy_audio(root / "present.mp3")
        _insert_track(conn, PRESENT_AT_FILE_PATH.sid, str(present), content_hash=_sha(present))
        conn.execute(
            "INSERT INTO track_locations(stable_id, machine_id, kind, role, file_path, "
            "available, content_hash, created_at, updated_at) "
            "VALUES (?, ?, 'local', 'primary', ?, 1, ?, ?, ?)",
            (PRESENT_AT_FILE_PATH.sid, machine_id, str(present), _sha(present), ISO, ISO),
        )
        _record_checked(conn, PRESENT_AT_FILE_PATH.sid, "present", str(present))
        indexed.append(str(present))

        indexed.append(
            _seed_folder_only_track(
                conn,
                master,
                root,
                PRESENT_ONLY_AT_FOLDER_PATH.sid,
                str(PRESENT_ONLY_AT_FOLDER_PATH.vendor_id),
            )
        )

        gone = str(root / "gone.mp3")
        _insert_track(conn, ABSENT.sid, gone)
        _record_checked(conn, ABSENT.sid, "absent", gone)
        indexed.append(gone)

        _insert_track(conn, STREAMING.sid, None)
        _map_to_folder_path(
            conn, master, STREAMING.sid, str(STREAMING.vendor_id), "tidal:tracks:99560085"
        )
        _record_checked(conn, STREAMING.sid, "streaming", "tidal:tracks:99560085")

        unchecked = _copy_audio(root / "unchecked.mp3")
        _insert_track(conn, UNCHECKED.sid, str(unchecked))
        indexed.append(str(unchecked))

        awaiting = f"{UNMOUNTED_VOLUME}/Music/awaiting.mp3"
        _insert_track(conn, AWAITING_VOLUME.sid, None)
        _map_to_folder_path(conn, master, AWAITING_VOLUME.sid, str(AWAITING_VOLUME.vendor_id), awaiting)
        _record_checked(conn, AWAITING_VOLUME.sid, "absent", awaiting)

        for i, sid in enumerate(FOLDER_ONLY_SIDS):
            indexed.append(_seed_folder_only_track(conn, master, root, sid, str(9100 + i)))
        for sid in BROKEN_SIDS:
            dead = str(root / "dead" / f"{sid[:8]}.mp3")
            _insert_track(conn, sid, dead)
            _record_checked(conn, sid, "absent", dead)
            indexed.append(dead)

        _add_playlist(conn, "pl-parity", [case.sid for case in PARITY_CASES])
        _add_playlist(conn, "pl-folder-only", FOLDER_ONLY_SIDS)
        _add_playlist(conn, "pl-broken", BROKEN_SIDS)

        # A fresh path index, as the background refresher leaves it: the tree
        # spends zero stats and answers checked rows from here.
        path_index.upsert_rows(
            conn,
            path_index.resolver_namespace(data_dir),
            [(p, Path(p).stat().st_size if Path(p).is_file() else None) for p in indexed],
        )
        conn.commit()
        master.commit()
    finally:
        master.close()
        conn.close()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    assert REAL_AUDIO.is_file()
    assert not Path(UNMOUNTED_VOLUME).exists()
    data_dir = tmp_path / "data"
    state_path = data_dir / "state" / "state.db"
    state_path.parent.mkdir(parents=True)
    monkeypatch.setenv("MDT_DATA_DIR", str(data_dir))
    monkeypatch.setattr(rb_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "master.plain.db")
    rb_config._FILE_EXISTS_CACHE.clear()
    _stub_rb_vendor(monkeypatch)
    monkeypatch.setattr(
        "apps.webui.server.routes.playlists.rb_vendor.playlist_order_index", dict
    )
    monkeypatch.setattr(path_availability_refresh, "schedule", lambda _paths: None)
    _seed(state_path, data_dir)
    app = create_app(
        backend=SqliteBackend(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_path),
        mount_frontend=False,
    )
    with TestClient(app, raise_server_exceptions=False, base_url=TEST_HOST_BASE_URL) as tc:
        yield tc


# ----------------------------------------------------------------------------
# The three surfaces, read the way the UI reads them.


def _tree_available_count(client: TestClient, playlist_id: str) -> tuple[int, int]:
    response = client.get("/api/v1/playlists", params={"availability": "all"})
    assert response.status_code == 200, response.text
    (row,) = [p for p in response.json() if p["playlist_id"] == playlist_id]
    return row["available_count"], row["track_count"]


def _tree_status_by_sid(sids: list[str]) -> dict[str, str]:
    conn = state_db.open_rw(rb_config.STATE_DB)
    try:
        file_paths = dict(
            conn.execute(
                f"SELECT stable_id, file_path FROM tracks WHERE stable_id IN "
                f"({','.join('?' * len(sids))})",
                sids,
            ).fetchall()
        )
    finally:
        conn.close()
    return dict(track_rows.bulk_availability_for_playlist_summary(sids, file_paths))


def _row_status_by_sid(client: TestClient, playlist_id: str) -> dict[str, str]:
    response = client.get(f"/api/v1/playlists/{playlist_id}")
    assert response.status_code == 200, response.text
    return {t["stable_id"]: t["file_availability"] for t in response.json()["tracks"]}


def _served(client: TestClient, sid: str) -> bool:
    response = client.get(f"/api/v1/tracks/{sid}/audio", headers={"Range": "bytes=0-0"})
    if response.status_code in (200, 206):
        assert response.headers["content-type"].startswith("audio/")
        return True
    assert response.status_code in (404, 415, 503), (sid, response.status_code, response.text)
    return False


# ----------------------------------------------------------------------------


def test_tree_rows_and_deck_load_agree_on_what_plays(client: TestClient) -> None:
    """[if] tree, rows and deck load read one playlist [then] they agree on what plays, [else stop].

    Rows present == served exactly, every track the tree counts is served, the
    only served track a cold tree does not count is the unchecked one (pending,
    not present), and once rows have stated it the tree count == served."""
    sids = [case.sid for case in PARITY_CASES]
    tree = _tree_status_by_sid(sids)
    cold_count = _tree_available_count(client, "pl-parity")
    rows = _row_status_by_sid(client, "pl-parity")
    served = {sid for sid in sids if _served(client, sid)}

    expected_served = {case.sid for case in PARITY_CASES if case.plays}
    assert served == expected_served
    assert {sid for sid, status in rows.items() if status == "present"} == served
    tree_present = {sid for sid, status in tree.items() if status == "present"}
    assert tree_present == {case.sid for case in PARITY_CASES if case.plays and case.checked}
    assert tree[UNCHECKED.sid] == "AVAILABILITY_PENDING"
    assert cold_count == (len(tree_present), len(PARITY_CASES))
    # Once the rows have stated the unchecked track, the tree counts exactly
    # what the deck serves.
    assert _tree_available_count(client, "pl-parity") == (len(served), len(PARITY_CASES))
    # Each non-playing kind keeps its own honest label on the row wire.
    assert rows[ABSENT.sid] == "absent"
    assert rows[STREAMING.sid] == "streaming"
    assert rows[AWAITING_VOLUME.sid] == "awaiting_volume"


def test_folder_path_track_is_served_from_the_folder_path(client: TestClient) -> None:
    """[if] a track's only copy is its rekordbox FolderPath [then] the deck plays that file, [else stop].

    The machine is configured, and the response names the FolderPath source."""
    response = client.get(
        f"/api/v1/tracks/{PRESENT_ONLY_AT_FOLDER_PATH.sid}/audio",
        headers={"Range": "bytes=0-0"},
    )
    assert response.status_code == 206, response.text
    assert response.headers.get("x-audio-source") == "rekordbox-folder-path"
    # Control: a configured machine still refuses a track it has no copy of.
    refused = client.get(f"/api/v1/tracks/{ABSENT.sid}/audio")
    assert refused.status_code == 404
    assert refused.json()["detail"]["code"] == "CLOUD_ASSET_UNAVAILABLE"


def test_hide_broken_shows_a_folder_path_playlist_and_hides_an_unplayable_one(
    client: TestClient,
) -> None:
    """[if] hide-broken lists FolderPath-only and copyless playlists [then] it counts only real copies, [else stop].

    Every member at its FolderPath: available_count == track_count and every
    member serves. No member with a copy: available_count is 0 and none serves."""
    available, total = _tree_available_count(client, "pl-folder-only")
    assert (available, total) == (len(FOLDER_ONLY_SIDS), len(FOLDER_ONLY_SIDS))
    assert all(_served(client, sid) for sid in FOLDER_ONLY_SIDS)

    available, total = _tree_available_count(client, "pl-broken")
    assert (available, total) == (0, len(BROKEN_SIDS))
    assert not any(_served(client, sid) for sid in BROKEN_SIDS)
