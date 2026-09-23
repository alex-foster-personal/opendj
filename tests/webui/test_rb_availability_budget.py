"""Acceptance tests for issue #1037 / PERF-RB-01 path availability budgets.

[if] a listing exceeds its stat budget or calls pending present [then] fail, [else stop].
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.shared.state.writer import StateWriter
from apps.webui.server import path_availability_refresh
from apps.webui.server.app import create_app
from apps.webui.server.rb_vendor_pkg import availability, path_index, track_rows
from apps.webui.server.sqlite_backend import SqliteBackend

from .conftest import TEST_HOST_BASE_URL, _stub_rb_vendor

pytestmark = pytest.mark.requirement("PERF-RB-01")

ISO = "2026-04-17T10:00:00.000000Z"


def _configure_data_dir(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> Path:
    state_db_path = data_dir / "state" / "state.db"
    monkeypatch.setenv("MDT_DATA_DIR", str(data_dir))
    monkeypatch.setattr(rb_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(rb_config, "STATE_DB", state_db_path)
    rb_config._FILE_EXISTS_CACHE.clear()
    return state_db_path


def _seed_library(
    state_db_path: Path,
    *,
    track_count: int = 2,
    playlist_id: str = "pl-big",
) -> tuple[list[str], list[str]]:
    music_dir = state_db_path.parent.parent / "music"
    music_dir.mkdir(parents=True, exist_ok=True)
    conn = state_db.open_rw(state_db_path)
    try:
        state_schema.apply_migrations(conn)
        sids: list[str] = []
        paths: list[str] = []
        for i in range(track_count):
            sid = f"sid-{i:04d}"
            path = music_dir / f"track-{i:04d}.mp3"
            path.write_bytes(b"ID3" + b"\x00" * 128)
            sids.append(sid)
            paths.append(str(path))
            conn.execute(
                "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, "
                "album, isrc, duration_ms, file_path, content_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    "fingerprint",
                    f"Track {i}",
                    json.dumps([f"Artist {i}"]),
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
            (playlist_id, "Big Set", "rekordbox", "RB-BIG", ISO, ISO),
        )
        for pos, sid in enumerate(sids):
            conn.execute(
                "INSERT INTO playlist_memberships(playlist_id, stable_id, position) "
                "VALUES (?, ?, ?)",
                (playlist_id, sid, pos),
            )
        conn.commit()
    finally:
        conn.close()
    return sids, paths


def _seed_index(
    state_db_path: Path,
    data_dir: Path,
    rows: list[tuple[str, int | None]],
    *,
    stale: bool,
) -> None:
    namespace = path_index.resolver_namespace(data_dir)
    checked_at = datetime.now(UTC) - timedelta(seconds=120 if stale else 5)
    conn = state_db.open_rw(state_db_path)
    try:
        path_index.upsert_rows(
            conn,
            namespace,
            rows,
        )
        for path, _size in rows:
            conn.execute(
                "UPDATE path_availability SET checked_at = ? "
                "WHERE resolver_namespace = ? AND logical_path = ?",
                (
                    checked_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    namespace,
                    path,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _stat_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"n": 0}
    real = availability._stat_size

    def counting(resolved: Path | None) -> int | None:
        calls["n"] += 1
        return real(resolved)

    monkeypatch.setattr(availability, "_stat_size", counting)
    monkeypatch.setattr(path_availability_refresh, "schedule", lambda _paths: None)
    return calls


@pytest.fixture
def availability_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Path, Path]]:
    data_dir = tmp_path / "data"
    state_db_path = _configure_data_dir(monkeypatch, data_dir)
    _stub_rb_vendor(monkeypatch)
    monkeypatch.setattr(
        "apps.webui.server.routes.playlists.rb_vendor.playlist_order_index",
        dict,
    )
    app = create_app(
        backend=SqliteBackend(state_db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_db_path),
        mount_frontend=False,
    )
    with TestClient(app, base_url=TEST_HOST_BASE_URL) as client:
        yield client, data_dir, state_db_path


@pytest.mark.requirement("PERF-RB-01")
def test_list_playlists_all_stale_index_zero_stats(
    availability_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] tree summary with all-stale index [then ⛔️] zero filesystem stats."""
    client, data_dir, state_db_path = availability_client
    _sids, paths = _seed_library(state_db_path, track_count=3)
    _seed_index(
        state_db_path,
        data_dir,
        [(path, 256) for path in paths],
        stale=True,
    )
    calls = _stat_spy(monkeypatch)

    response = client.get("/api/v1/playlists", params={"availability": "all"})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["available_count"] == 3
    assert calls["n"] == 0


@pytest.mark.requirement("PERF-RB-01")
def test_playlist_detail_500_stale_caps_at_16_stats(
    availability_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] playlist detail with 500 stale rows [then ⛔️] at most 16 stats."""
    client, data_dir, state_db_path = availability_client
    _sids, paths = _seed_library(state_db_path, track_count=500)
    _seed_index(
        state_db_path,
        data_dir,
        [(path, 256) for path in paths],
        stale=True,
    )
    calls = _stat_spy(monkeypatch)

    response = client.get("/api/v1/playlists/pl-big")

    assert response.status_code == 200
    tracks = response.json()["tracks"]
    assert len(tracks) == 500
    assert calls["n"] == 16
    pending = [t for t in tracks if t["file_availability"] == "AVAILABILITY_PENDING"]
    assert len(pending) == 484
    assert all(t["file_exists"] is None for t in pending)


@pytest.mark.requirement("PERF-RB-01")
def test_restart_serves_index_before_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] server restarts [then ⛔️] index answers are served before any stat."""
    data_dir = tmp_path / "data"
    state_db_path = _configure_data_dir(monkeypatch, data_dir)
    _stub_rb_vendor(monkeypatch)
    monkeypatch.setattr(
        "apps.webui.server.routes.playlists.rb_vendor.playlist_order_index",
        dict,
    )
    _sids, paths = _seed_library(state_db_path, track_count=4)
    _seed_index(
        state_db_path,
        data_dir,
        [(path, 256) for path in paths],
        stale=False,
    )
    calls = _stat_spy(monkeypatch)

    app = create_app(
        backend=SqliteBackend(state_db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_db_path),
        mount_frontend=False,
    )
    path_availability_refresh.stop()
    with TestClient(app, base_url=TEST_HOST_BASE_URL) as client:
        response = client.get("/api/v1/playlists/pl-big")

    assert response.status_code == 200
    tracks = response.json()["tracks"]
    assert all(t["file_availability"] == "present" for t in tracks)
    assert calls["n"] == 0


@pytest.mark.requirement("PERF-RB-01")
def test_pending_never_sets_file_exists_bool(
    availability_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] row is pending [then ⛔️] response marks AVAILABILITY_PENDING only."""
    client, data_dir, state_db_path = availability_client
    _sids, paths = _seed_library(state_db_path, track_count=20)
    _seed_index(
        state_db_path,
        data_dir,
        [(path, 256) for path in paths],
        stale=True,
    )
    _stat_spy(monkeypatch)

    response = client.get("/api/v1/playlists/pl-big")

    assert response.status_code == 200
    pending = next(
        t for t in response.json()["tracks"]
        if t["file_availability"] == "AVAILABILITY_PENDING"
    )
    assert pending["file_exists"] is None


@pytest.mark.requirement("PERF-RB-01")
def test_bulk_file_size_tree_summary_never_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unit-level guard: TREE_SUMMARY budget is zero stats even on index miss."""
    data_dir = tmp_path / "data"
    state_db_path = _configure_data_dir(monkeypatch, data_dir)
    music_dir = data_dir / "music"
    music_dir.mkdir(parents=True)
    path = music_dir / "one.mp3"
    path.write_bytes(b"ID3")
    conn = state_db.open_rw(state_db_path)
    try:
        state_schema.apply_migrations(conn)
        conn.commit()
    finally:
        conn.close()
    calls = _stat_spy(monkeypatch)

    probed = track_rows.bulk_probe_paths(
        [str(path)],
        probe_mode=track_rows.AvailabilityProbeMode.TREE_SUMMARY,
    )

    assert calls["n"] == 0
    assert probed[str(path)].status == "AVAILABILITY_PENDING"


def _seed_alternates(state_db_path: Path, sids: list[str], *, present: bool) -> list[str]:
    """Point each track's primary path at a missing file and give it one
    ``track_locations`` alternate (a real file when ``present``)."""
    alt_dir = state_db_path.parent.parent / "alternates"
    alt_dir.mkdir(parents=True, exist_ok=True)
    alt_paths: list[str] = []
    conn = state_db.open_rw(state_db_path)
    try:
        writer = StateWriter(conn, actor="unit-test")
        for sid in sids:
            alt = alt_dir / f"{sid}.mp3"
            if present:
                alt.write_bytes(b"ID3" + b"\x00" * 64)
            alt_paths.append(str(alt))
            conn.execute(
                "UPDATE tracks SET file_path = ? WHERE stable_id = ?",
                (str(alt_dir / "gone" / f"{sid}.mp3"), sid),
            )
            writer.upsert_track_location(stable_id=sid, kind="local", file_path=str(alt))
        writer.close()
        conn.commit()
    finally:
        conn.close()
    return alt_paths


def _scheduled_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    scheduled: list[str] = []
    monkeypatch.setattr(path_availability_refresh, "schedule", scheduled.extend)
    return scheduled


@pytest.mark.requirement("PERF-RB-01")
def test_reconcile_scan_over_budget_never_calls_present_files_broken(
    availability_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] the broken-link scan meets more cold paths than one listing budget
    [then ⛔️] every one is still stat'ed and none present is reported broken."""
    client, _data_dir, state_db_path = availability_client
    _sids, paths = _seed_library(state_db_path, track_count=40)
    Path(paths[0]).unlink()  # control: one genuinely missing file
    calls = _stat_spy(monkeypatch)

    response = client.get("/api/v1/reconcile/broken")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [t["original_path"] for t in body["tracks"]] == [paths[0]]
    assert calls["n"] == 40


@pytest.mark.requirement("PERF-RB-01")
def test_alternates_share_the_row_hydration_budget(
    availability_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] primaries AND track_locations alternates are cold [then ⛔️] one
    playlist request still spends at most 16 stats and calls nothing absent."""
    client, _data_dir, state_db_path = availability_client
    sids, _paths = _seed_library(state_db_path, track_count=20)
    _seed_alternates(state_db_path, sids, present=True)
    calls = _stat_spy(monkeypatch)

    response = client.get("/api/v1/playlists/pl-big")

    assert response.status_code == 200, response.text
    tracks = response.json()["tracks"]
    assert calls["n"] == 16
    assert {t["file_availability"] for t in tracks} == {"AVAILABILITY_PENDING"}
    assert all(t["file_exists"] is None for t in tracks)


@pytest.mark.requirement("PERF-RB-01")
def test_present_alternate_wins_within_budget(
    availability_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] a row's primary is gone but its alternate is on disk [then ⛔️] it
    is present, and a row with no copy anywhere is absent (control)."""
    client, _data_dir, state_db_path = availability_client
    sids, _paths = _seed_library(state_db_path, track_count=4)
    _seed_alternates(state_db_path, sids[:2], present=True)
    _seed_alternates(state_db_path, sids[2:], present=False)
    _stat_spy(monkeypatch)

    response = client.get("/api/v1/playlists/pl-big")

    assert response.status_code == 200, response.text
    by_sid = {t["stable_id"]: t for t in response.json()["tracks"]}
    assert [by_sid[sid]["file_availability"] for sid in sids] == [
        "present", "present", "absent", "absent",
    ]
    assert [by_sid[sid]["file_exists"] for sid in sids] == [True, True, False, False]


@pytest.mark.requirement("PERF-RB-01")
def test_tree_summary_refreshes_stale_entries_in_background(
    availability_client: tuple[TestClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] the tree summary serves a stale index answer [then ⛔️] that path is
    queued for refresh, so the TTL still bounds how long the count can lag."""
    client, data_dir, state_db_path = availability_client
    _sids, paths = _seed_library(state_db_path, track_count=3)
    _seed_index(state_db_path, data_dir, [(p, 256) for p in paths[:2]], stale=True)
    _seed_index(state_db_path, data_dir, [(paths[2], 256)], stale=False)
    calls = _stat_spy(monkeypatch)
    scheduled = _scheduled_spy(monkeypatch)

    response = client.get("/api/v1/playlists", params={"availability": "all"})

    assert response.status_code == 200, response.text
    assert response.json()[0]["available_count"] == 3
    assert calls["n"] == 0
    assert sorted(scheduled) == sorted(paths[:2])


@pytest.mark.requirement("PERF-RB-01")
def test_file_exists_is_null_only_while_pending() -> None:
    """[if] a settled status [then ⛔️] file_exists is a bool; null only for pending."""
    assert availability.status_to_file_exists("present") is True
    for status in ("absent", "streaming", "awaiting_volume"):
        assert availability.status_to_file_exists(status) is False
    assert availability.status_to_file_exists("AVAILABILITY_PENDING") is None
