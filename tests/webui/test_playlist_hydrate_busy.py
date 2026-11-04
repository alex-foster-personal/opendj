"""STATE-5450: listing GET must not 503 when path_availability persist is locked.

[if] writer lock is held [then] GET playlist still 200 with disk truth, [else stop].
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.webui.server.app import create_app
from apps.webui.server.rb_vendor_pkg import availability, path_index
from apps.webui.server.sqlite_backend import SqliteBackend

from .app_fixtures import _stub_rb_vendor
from .conftest import TEST_HOST_BASE_URL

pytestmark = pytest.mark.requirement("STATE-5450")

ISO = "2026-04-17T10:00:00.000000Z"


def _configure_data_dir(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> Path:
    state_db_path = data_dir / "state" / "state.db"
    monkeypatch.setenv("MDT_DATA_DIR", str(data_dir))
    monkeypatch.setattr(rb_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(rb_config, "STATE_DB", state_db_path)
    rb_config._FILE_EXISTS_CACHE.clear()
    return state_db_path


def _seed_one_track(state_db_path: Path) -> str:
    music_dir = state_db_path.parent.parent / "music"
    music_dir.mkdir(parents=True, exist_ok=True)
    path = music_dir / "track-0000.mp3"
    path.write_bytes(b"ID3" + b"\x00" * 128)
    conn = state_db.open_rw(state_db_path)
    try:
        state_schema.apply_migrations(conn)
        conn.execute(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, "
            "album, isrc, duration_ms, file_path, content_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "sid-0000",
                "fingerprint",
                "Track 0",
                json.dumps(["Artist 0"]),
                None,
                None,
                180_000,
                str(path),
                None,
                ISO,
                ISO,
            ),
        )
        conn.execute(
            "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("pl-busy", "Busy Set", "rekordbox", "RB-BUSY", ISO, ISO),
        )
        conn.execute(
            "INSERT INTO playlist_memberships(playlist_id, stable_id, position) "
            "VALUES (?, ?, ?)",
            ("pl-busy", "sid-0000", 0),
        )
        conn.commit()
    finally:
        conn.close()
    return str(path)


@pytest.fixture
def hydrate_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Path, Path]]:
    data_dir = tmp_path / "data"
    state_db_path = _configure_data_dir(monkeypatch, data_dir)
    _stub_rb_vendor(monkeypatch)
    monkeypatch.setattr(
        "apps.webui.server.routes.playlists.rb_vendor.playlist_order_index",
        dict,
    )
    _seed_one_track(state_db_path)
    app = create_app(
        backend=SqliteBackend(state_db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_db_path),
        mount_frontend=False,
    )
    with TestClient(app, base_url=TEST_HOST_BASE_URL) as client:
        yield client, data_dir, state_db_path


def test_bulk_probe_keeps_l1_when_path_index_writer_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] hydrate persist is locked [then] the stat still answers L1, [else stop]."""
    data_dir = tmp_path / "data"
    state_db_path = _configure_data_dir(monkeypatch, data_dir)
    path = _seed_one_track(state_db_path)
    holder = state_db.open_rw(state_db_path, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        probed = availability.bulk_probe_paths(
            [path],
            probe_mode=availability.AvailabilityProbeMode.ROW_HYDRATION,
        )
        elapsed = time.monotonic() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert probed[path].status == "present"
    assert elapsed < 2.0
    with rb_config._FILE_EXISTS_LOCK:
        cached = rb_config._FILE_EXISTS_CACHE.get(path)
    assert cached is not None
    assert cached[1] == probed[path].materialised_size
    conn = state_db.open_ro(state_db_path)
    try:
        index = path_index.bulk_lookup(
            conn, path_index.resolver_namespace(data_dir), [path]
        )
    finally:
        conn.close()
    assert index[path] is None


def test_bulk_probe_persists_path_index_when_writer_is_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] writer lock is free [then] hydrate persist writes the index, [else stop]."""
    data_dir = tmp_path / "data"
    state_db_path = _configure_data_dir(monkeypatch, data_dir)
    path = _seed_one_track(state_db_path)
    probed = availability.bulk_probe_paths(
        [path],
        probe_mode=availability.AvailabilityProbeMode.ROW_HYDRATION,
    )
    assert probed[path].status == "present"
    conn = state_db.open_ro(state_db_path)
    try:
        index = path_index.bulk_lookup(
            conn, path_index.resolver_namespace(data_dir), [path]
        )
    finally:
        conn.close()
    assert index[path] is not None
    assert index[path].materialised_size == probed[path].materialised_size


def test_get_playlist_200_while_writer_holds_lock(
    hydrate_client: tuple[TestClient, Path, Path],
) -> None:
    """[if] GET playlist hydrate persist is busy [then] 200 not 503, [else stop]."""
    client, _data_dir, state_db_path = hydrate_client
    holder = state_db.open_rw(state_db_path, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        response = client.get("/api/v1/playlists/pl-busy")
        elapsed = time.monotonic() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert response.status_code == 200, response.text
    tracks = response.json()["tracks"]
    assert tracks[0]["file_availability"] == "present"
    assert elapsed < 2.0
