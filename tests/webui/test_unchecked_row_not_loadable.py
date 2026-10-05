"""Unchecked library rows must not look playable when the file is missing.

A fresh path_availability index copied with state.db can say an Air path is
present. With no track_availability row, the listing has to stat that path
on this machine. A missing file is absent. A file that is here stays present.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state.locations import ID_BIND_BATCH
from apps.webui.server.backend import InMemoryBackend, Track
from apps.webui.server.rb_vendor_pkg import availability
from apps.webui.server.rb_vendor_pkg.track_rows import (
    bulk_availability_for_playlist_summary,
    bulk_availability_status,
)
from apps.webui.server.sqlite_backend import SqliteBackend

from .test_rb_availability_budget import ISO, _configure_data_dir, _seed_index, _seed_library

pytestmark = pytest.mark.requirement("LIBM-167")


def _listing_status(state_db_path: Path, sid: str, path: str) -> str:
    availability_status = bulk_availability_status([sid], {sid: path})
    return availability_status[sid]


# REQ: LIBM-167
@pytest.mark.requirement("LIBM-167")
def test_unchecked_missing_path_is_not_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] an unchecked row's local path is missing [then] it is not present."""
    data_dir = tmp_path / "data"
    state_db_path = _configure_data_dir(monkeypatch, data_dir)
    sids, paths = _seed_library(state_db_path, track_count=1)
    missing = Path(paths[0])
    missing.unlink()
    _seed_index(state_db_path, data_dir, [(str(missing), 256)], stale=False)

    status = _listing_status(state_db_path, sids[0], str(missing))

    assert status != "present"
    assert availability.status_to_file_exists(status) is not True


# REQ: LIBM-167
@pytest.mark.requirement("LIBM-167")
def test_present_file_still_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the local file is on disk [then] the row stays present."""
    data_dir = tmp_path / "data"
    state_db_path = _configure_data_dir(monkeypatch, data_dir)
    sids, paths = _seed_library(state_db_path, track_count=1)
    _seed_index(state_db_path, data_dir, [(paths[0], 256)], stale=False)

    status = _listing_status(state_db_path, sids[0], paths[0])

    assert status == "present"
    assert availability.status_to_file_exists(status) is True


# REQ: LIBM-167
@pytest.mark.requirement("LIBM-167")
def test_streaming_uri_is_not_playable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a tidal URI is stored as streaming [then] playable counts present only."""
    data_dir = tmp_path / "data"
    state_db_path = _configure_data_dir(monkeypatch, data_dir)
    sids, paths = _seed_library(state_db_path, track_count=2)
    tidal = "tidal:tracks:99560085"
    conn = state_db.open_rw(state_db_path)
    try:
        conn.execute(
            "UPDATE tracks SET file_path = ? WHERE stable_id = ?",
            (tidal, sids[1]),
        )
        conn.execute(
            "INSERT INTO track_availability(stable_id, state, checked_path, checked_at) "
            "VALUES (?, 'present', ?, ?)",
            (sids[0], paths[0], ISO),
        )
        conn.execute(
            "INSERT INTO track_availability(stable_id, state, checked_path, checked_at) "
            "VALUES (?, 'streaming', ?, ?)",
            (sids[1], tidal, ISO),
        )
        conn.commit()
    finally:
        conn.close()

    stats = SqliteBackend(state_db_path).stats()
    classified = availability.classify_availability(tidal, [], awaiting_volume=False)

    assert classified == "streaming"
    assert stats["tracks"] == 2
    assert stats["tracks_playable"] == 1


# REQ: LIBM-167
@pytest.mark.requirement("LIBM-167")
def test_unchecked_lookup_pages_under_the_real_sqlite_variable_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] membership exceeds the connection's bind limit [then] known ids stay known.

    One IN (...) per id raises "too many SQL variables" on the packaged
    SQLite (limit 999). The lookup then used to treat the whole playlist as
    unchecked. This pins the engine limit itself, lowered on the connection
    the lookup opens, so the failure is a real SQLite rejection.
    """
    data_dir = tmp_path / "data"
    state_db_path = _configure_data_dir(monkeypatch, data_dir)
    track_count = ID_BIND_BATCH + 25
    sids, paths = _seed_library(state_db_path, track_count=track_count)
    known = sids[0]
    conn = state_db.open_rw(state_db_path)
    try:
        conn.execute(
            "INSERT INTO track_availability(stable_id, state, checked_path, checked_at) "
            "VALUES (?, 'present', ?, ?)",
            (known, paths[0], ISO),
        )
        conn.commit()
    finally:
        conn.close()

    real_open = availability._open_ro

    def limited(path: Path, label: str) -> sqlite3.Connection:
        opened = real_open(path, label)
        opened.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, ID_BIND_BATCH)
        return opened

    monkeypatch.setattr(availability, "_open_ro", limited)

    missing = availability.sids_without_availability_row(sids)

    assert known not in missing
    assert len(missing) == track_count - 1


# REQ: LIBM-167
@pytest.mark.requirement("LIBM-167")
def test_tree_summary_does_not_count_an_unchecked_index_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a copied index says present and there is no availability row
    [then] the tree does not count the track, and it does not stat."""
    data_dir = tmp_path / "data"
    state_db_path = _configure_data_dir(monkeypatch, data_dir)
    sids, paths = _seed_library(state_db_path, track_count=1)
    missing = Path(paths[0])
    missing.unlink()
    _seed_index(state_db_path, data_dir, [(str(missing), 256)], stale=False)
    stats: list[str] = []

    def _no_stat(path: str) -> int | None:
        stats.append(path)
        return None

    monkeypatch.setattr(availability, "_stat_size", _no_stat)

    summary = bulk_availability_for_playlist_summary(sids, {sids[0]: str(missing)})

    assert summary[sids[0]] != "present"
    assert stats == []


# REQ: LIBM-167
@pytest.mark.requirement("LIBM-167")
def test_in_memory_health_does_not_call_an_unconfirmed_track_playable() -> None:
    """[if] the backend has no track_availability row [then] playable is 0."""
    backend = InMemoryBackend()
    backend.seed_track(Track(stable_id="s1", file_path="/tmp/not-here.mp3"))

    stats = backend.stats()

    assert stats["tracks"] == 1
    assert stats["tracks_playable"] == 0
