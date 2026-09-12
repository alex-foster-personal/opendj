"""Pre-v7 state.db readers: search, ingest coverage, crate_sync (issue #730)."""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.webui import crate_sync
from apps.webui.server import search_index
from apps.webui.server.routes import ingest as ingest_mod
from apps.webui.server.routes.ingest_job import tracks_on_disk
from apps.webui.soft_deletes import has_soft_deletes
from tests.webui.pre_v7_state import (
    PLAYLIST_NAME,
    TRACK_STABLE_ID,
    assert_no_deleted_at_column,
    build_pre_v7_state_db,
)


@pytest.fixture
def pre_v7_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "state.db"
    build_pre_v7_state_db(path)
    conn = sqlite3.connect(path)
    try:
        for table in ("tracks", "playlists", "playlist_memberships"):
            assert_no_deleted_at_column(conn, table)
        membership_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(playlist_memberships)")
        }
        assert "order_key" not in membership_columns
        version = conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
        assert version == 6
    finally:
        conn.close()
    return path


def test_has_soft_deletes_false_on_v6(pre_v7_path: Path) -> None:
    conn = sqlite3.connect(pre_v7_path)
    try:
        assert has_soft_deletes(conn, "tracks") is False
        assert has_soft_deletes(conn, "playlists") is False
        assert has_soft_deletes(conn, "playlist_memberships") is False
        with pytest.raises(ValueError, match="unsupported table"):
            has_soft_deletes(conn, "sync_policies")
    finally:
        conn.close()


def test_search_read_source_and_search_on_v6(pre_v7_path: Path, tmp_path: Path) -> None:
    tracks, _eav = search_index._read_source(pre_v7_path)
    assert tracks[0]["stable_id"] == TRACK_STABLE_ID

    index_path = tmp_path / "search-index.sqlite"
    hits, total = search_index.search(
        pre_v7_path, "Time", limit=10, index_db_path=index_path
    )
    assert total == 1
    assert hits[0][0] == TRACK_STABLE_ID


def test_tracks_on_disk_on_v6(pre_v7_path: Path) -> None:
    on_disk, unreachable = tracks_on_disk(lambda: sqlite3.connect(pre_v7_path))
    assert on_disk == []
    assert unreachable == 1


def test_get_coverage_on_v6(pre_v7_path: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ingest_mod, "CONFIG_PATH", tmp_path / "ingest-config.json")
    monkeypatch.setattr(ingest_mod, "INGEST_INBOX", tmp_path / "_ingest")
    monkeypatch.setattr(ingest_mod, "VOCAL_CACHE_DIR", tmp_path / "vocal-cache")
    monkeypatch.setattr(ingest_mod, "LYRICS_CACHE_DIR", tmp_path / "lyrics-cache")
    monkeypatch.setattr(ingest_mod, "DEFAULT_STEMS_DIR", tmp_path / "stems")
    monkeypatch.setattr(ingest_mod, "open_ro", lambda: sqlite3.connect(pre_v7_path))

    app = FastAPI()
    app.include_router(ingest_mod.router, prefix="/api/v1")
    client = TestClient(app)
    response = client.get("/api/v1/ingest/coverage")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total_tracks"] == 1
    assert payload["unreachable"] == 1


def test_crate_sync_playlist_lookup_on_v6(pre_v7_path: Path) -> None:
    conn = sqlite3.connect(pre_v7_path)
    try:
        traced_sql: list[str] = []

        def _trace(sql: str) -> None:
            traced_sql.append(sql)

        conn.set_trace_callback(_trace)
        assert crate_sync._playlist_stable_ids(conn, PLAYLIST_NAME) == (
            TRACK_STABLE_ID,
        )
        assert not any("order_key" in statement for statement in traced_sql)
        with pytest.raises(RuntimeError, match="unknown playlist"):
            crate_sync._playlist_stable_ids(conn, "Missing Set")
    finally:
        conn.close()


def test_crate_sync_playlist_lookup_orders_by_order_key_on_v13(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, actor="crate-sync-order-key")
    try:
        for sid, title in (("sid-a", "A"), ("sid-b", "B")):
            writer.upsert_track(
                stable_id=sid,
                stable_id_tier="inferred",
                title=title,
                artists=[],
                album=None,
                isrc=None,
                duration_ms=None,
                file_path=f"/music/{sid}.mp3",
            )
        writer.insert_playlist(
            playlist_id="pl-keyed",
            name="Keyed Set",
            vendor="rekordbox",
            vendor_pl_id="rb-keyed",
        )
        writer.insert_playlist_memberships(
            "pl-keyed",
            [
                ("item-a", "sid-a", "zzzzzzzz"),
                ("item-b", "sid-b", "aaaaaaaa"),
            ],
        )
    finally:
        writer.close()
        conn.close()

    ro = sqlite3.connect(path)
    try:
        traced_sql: list[str] = []

        def _trace(sql: str) -> None:
            traced_sql.append(sql)

        ro.set_trace_callback(_trace)
        assert crate_sync._playlist_stable_ids(ro, "Keyed Set") == (
            "sid-b",
            "sid-a",
        )
        assert any("order_key" in statement for statement in traced_sql)
    finally:
        ro.close()


def test_crate_sync_collect_plan_on_v6(pre_v7_path: Path, tmp_path: Path) -> None:
    crate_root = tmp_path / "crate"
    crate_root.mkdir()
    user_maps = (("/Users/dev", str(crate_root / "Contents")),)

    for playlist in (PLAYLIST_NAME, None):
        plan = crate_sync.collect_plan(
            state_db=pre_v7_path,
            master_db=None,
            crate_root=crate_root,
            user_maps=user_maps,
            playlist=playlist,
        )
        assert plan.skipped_absent == 1


def test_soft_deletes_filter_after_v7_migration(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "state-v6.db"
    build_pre_v7_state_db(source)
    migrated = tmp_path / "state-v7.db"
    shutil.copy2(source, migrated)
    state_db.open_rw(migrated).close()

    conn = sqlite3.connect(migrated)
    try:
        assert has_soft_deletes(conn, "tracks") is True
        tombstone = "2026-04-17T11:00:00.000000Z"
        conn.execute(
            "UPDATE tracks SET deleted_at = ? WHERE stable_id = ?",
            (tombstone, TRACK_STABLE_ID),
        )
        conn.execute(
            "UPDATE playlists SET deleted_at = ? WHERE playlist_id = ?",
            (tombstone, "pl-old"),
        )
        conn.commit()
    finally:
        conn.close()

    tracks, _eav = search_index._read_source(migrated)
    assert not any(row["stable_id"] == TRACK_STABLE_ID for row in tracks)

    index_path = tmp_path / "search-index.sqlite"
    hits, total = search_index.search(
        migrated, "Time", limit=10, index_db_path=index_path
    )
    assert total == 0
    assert hits == []

    conn = sqlite3.connect(migrated)
    try:
        with pytest.raises(RuntimeError, match="unknown playlist"):
            crate_sync._playlist_stable_ids(conn, PLAYLIST_NAME)
    finally:
        conn.close()

    monkeypatch_conn = migrated
    on_disk, unreachable = tracks_on_disk(lambda: sqlite3.connect(monkeypatch_conn))
    assert on_disk == []
    assert unreachable == 0

    monkeypatch.setattr(ingest_mod, "CONFIG_PATH", tmp_path / "ingest-config.json")
    monkeypatch.setattr(ingest_mod, "INGEST_INBOX", tmp_path / "_ingest")
    monkeypatch.setattr(ingest_mod, "VOCAL_CACHE_DIR", tmp_path / "vocal-cache")
    monkeypatch.setattr(ingest_mod, "LYRICS_CACHE_DIR", tmp_path / "lyrics-cache")
    monkeypatch.setattr(ingest_mod, "DEFAULT_STEMS_DIR", tmp_path / "stems")
    monkeypatch.setattr(ingest_mod, "open_ro", lambda: sqlite3.connect(migrated))

    app = FastAPI()
    app.include_router(ingest_mod.router, prefix="/api/v1")
    client = TestClient(app)
    response = client.get("/api/v1/ingest/coverage")
    assert response.status_code == 200
    assert response.json()["total_tracks"] == 0

    plan = crate_sync.collect_plan(
        state_db=migrated,
        master_db=None,
        crate_root=tmp_path / "crate",
        user_maps=(("/Users/dev", str(tmp_path / "crate" / "Contents")),),
        playlist=None,
    )
    assert plan.skipped_absent == 0
