"""Unchecked library rows must not look playable when the file is missing.

A fresh path_availability index copied with state.db can say an Air path is
present. With no track_availability row, the listing has to stat that path
on this machine. A missing file is absent. A file that is here stays present.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.webui.server.rb_vendor_pkg import availability
from apps.webui.server.rb_vendor_pkg.track_rows import bulk_availability_status
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
