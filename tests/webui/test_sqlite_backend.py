"""Tests for :mod:`apps.webui.server.sqlite_backend`.

Covers:
  * round-trip reads against a freshly-initialized Phase 5 ``state.db``
    (tracks, playlists, playlist memberships, track_fields projection);
  * fallback paths for tables Phase 5 does not ship yet (``pairings``,
    queues) and the once-per-process warning cache;
  * :func:`make_backend` file-exists gating.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.webui.server.backend import (
    InMemoryBackend, NotFoundError, Pairing, QueueItem, Track, TrackFilter,
)
from apps.webui.server import sqlite_backend as sb_mod
from apps.webui.server.sqlite_backend import SqliteBackend, make_backend

pytestmark = pytest.mark.requirement("CAT-05")


ISO = "2026-04-17T10:00:00.000000Z"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture(autouse=True)
def _reset_warnings() -> None:
    sb_mod._reset_warnings_for_tests()


@pytest.fixture
def fresh_state_db(tmp_path: Path) -> Path:
    """Build a Phase 5 ``state.db`` with a known fixture population."""
    db_path = tmp_path / "state.db"
    conn = state_db.open_rw(db_path)
    try:
        for sid, tier, title, artists, album, isrc, dur, fp in [
            ("sid-001", "isrc", "Midnight Drive",
             ["the maintainer"], "Drive", "USAAA0000001", 240_000,
             "/music/midnight.mp3"),
            ("sid-002", "fingerprint", "Oxide",
             ["Beta", "Gamma"], "Minerals", None, 312_500,
             "/music/oxide.mp3"),
            ("sid-003", "inferred", "Gulf",
             ["Gamma"], "Ambient 1", None, 600_000, "/music/gulf.flac"),
        ]:
            conn.execute(
                "INSERT INTO tracks(stable_id, stable_id_tier, title, "
                "  artists_json, album, isrc, duration_ms, file_path, "
                "  content_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, tier, title, json.dumps(artists), album, isrc, dur, fp,
                 None, ISO, ISO),
            )
        # EAV fields on sid-001
        for field, val, source, conf in [
            ("bpm", 124.0, "rekordbox", 0.95),
            ("key", "8A", "rekordbox", 1.0),
            ("rating", 4, "rekordbox", 1.0),
            ("tags", ["deep-house", "smooth"], "manual", 1.0),
            ("notes", "great opener", "manual", None),
        ]:
            conn.execute(
                "INSERT INTO track_fields(stable_id, field_name, value_json, "
                "  source, confidence, modified_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("sid-001", field, json.dumps(val), source, conf, ISO),
            )
        # sid-002: only bpm
        conn.execute(
            "INSERT INTO track_fields(stable_id, field_name, value_json, "
            "  source, confidence, modified_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("sid-002", "bpm", json.dumps(128.0), "rekordbox", 0.9, ISO),
        )
        # Playlists
        conn.execute(
            "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, "
            "  created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("pl-001", "Opener Set", "rekordbox", "RB-1", ISO, ISO),
        )
        conn.execute(
            "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, "
            "  created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("pl-002", "Peak Hour", "djay", "DJ-1", ISO, ISO),
        )
        for pid, sid, pos in [
            ("pl-001", "sid-003", 0),
            ("pl-001", "sid-001", 1),
            ("pl-002", "sid-001", 0),
            ("pl-002", "sid-002", 1),
        ]:
            conn.execute(
                "INSERT INTO playlist_memberships(playlist_id, stable_id, "
                "  position) VALUES (?, ?, ?)",
                (pid, sid, pos),
            )
    finally:
        conn.close()
    return db_path


# --- round-trip reads ----------------------------------------------------

class TestRoundTripReads:
    def test_list_tracks_projects_eav_fields(
        self, fresh_state_db: Path,
    ) -> None:
        backend = SqliteBackend(fresh_state_db)
        page = backend.list_tracks(TrackFilter())
        assert page.next_cursor is None
        assert [t.stable_id for t in page.items] == [
            "sid-001", "sid-002", "sid-003",
        ]
        t1 = page.items[0]
        assert t1.title == "Midnight Drive"
        assert t1.artist == "the maintainer"
        assert t1.album == "Drive"
        assert t1.duration_ms == 240_000
        assert t1.bpm == 124.0
        assert t1.key == "8A"
        assert t1.rating == 4
        assert t1.tags == ["deep-house", "smooth"]
        assert t1.notes == "great opener"
        assert "bpm" in t1.provenance
        assert t1.provenance["bpm"].source == "rekordbox"
        assert t1.provenance["bpm"].confidence == 0.95
        # sid-002 multi-artist join
        t2 = page.items[1]
        assert t2.artist == "Beta, Gamma"
        assert t2.bpm == 128.0
        assert t2.rating is None
        assert t2.tags == []

    def test_list_tracks_filters(self, fresh_state_db: Path) -> None:
        backend = SqliteBackend(fresh_state_db)
        q_page = backend.list_tracks(TrackFilter(q="midnight"))
        assert [t.stable_id for t in q_page.items] == ["sid-001"]

        bpm_page = backend.list_tracks(
            TrackFilter(bpm_min=125.0, bpm_max=130.0),
        )
        assert [t.stable_id for t in bpm_page.items] == ["sid-002"]

        tag_page = backend.list_tracks(TrackFilter(tag="deep-house"))
        assert [t.stable_id for t in tag_page.items] == ["sid-001"]

        rating_page = backend.list_tracks(TrackFilter(rating_min=3))
        assert [t.stable_id for t in rating_page.items] == ["sid-001"]

        key_page = backend.list_tracks(TrackFilter(key="8A"))
        assert [t.stable_id for t in key_page.items] == ["sid-001"]

    def test_list_tracks_pagination(self, fresh_state_db: Path) -> None:
        backend = SqliteBackend(fresh_state_db)
        first = backend.list_tracks(TrackFilter(limit=2))
        assert [t.stable_id for t in first.items] == ["sid-001", "sid-002"]
        assert first.next_cursor == "sid-002"
        second = backend.list_tracks(
            TrackFilter(limit=2, cursor=first.next_cursor),
        )
        assert [t.stable_id for t in second.items] == ["sid-003"]
        assert second.next_cursor is None

    def test_get_track_roundtrip(self, fresh_state_db: Path) -> None:
        backend = SqliteBackend(fresh_state_db)
        track = backend.get_track("sid-001")
        assert track.title == "Midnight Drive"
        assert track.bpm == 124.0
        assert track.provenance["tags"].value == [
            "deep-house", "smooth",
        ]

    def test_get_track_not_found(self, fresh_state_db: Path) -> None:
        backend = SqliteBackend(fresh_state_db)
        with pytest.raises(NotFoundError):
            backend.get_track("nope")

    def test_list_playlists_and_memberships(
        self, fresh_state_db: Path,
    ) -> None:
        backend = SqliteBackend(fresh_state_db)
        pls = backend.list_playlists()
        assert [p.playlist_id for p in pls] == ["pl-001", "pl-002"]
        pl1 = next(p for p in pls if p.playlist_id == "pl-001")
        assert pl1.vendor == "rekordbox"
        assert pl1.items == ["sid-003", "sid-001"]

    def test_get_playlist(self, fresh_state_db: Path) -> None:
        backend = SqliteBackend(fresh_state_db)
        pl = backend.get_playlist("pl-002")
        assert pl.name == "Peak Hour"
        assert pl.items == ["sid-001", "sid-002"]

    def test_get_playlist_not_found(self, fresh_state_db: Path) -> None:
        backend = SqliteBackend(fresh_state_db)
        with pytest.raises(NotFoundError):
            backend.get_playlist("missing")

    def test_stats(self, fresh_state_db: Path) -> None:
        backend = SqliteBackend(fresh_state_db)
        s = backend.stats()
        assert s == {"tracks": 3, "playlists": 2, "pairings": 0}


# --- fallbacks for Phase-5-missing entities ------------------------------

class TestFallbackPaths:
    def test_list_pairings_falls_back_with_warning(
        self, fresh_state_db: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        fallback = InMemoryBackend()
        now = _iso_now()
        fallback.seed_pairing(Pairing(
            pairing_id="p-1", from_stable_id="a", to_stable_id="b",
            direction="->", source="manual", notes=None,
            created_at=now, updated_at=now,
        ))
        backend = SqliteBackend(fresh_state_db, fallback=fallback)
        with caplog.at_level(logging.WARNING, logger=sb_mod.log.name):
            out = backend.list_pairings()
            # Second call: no new warning (once-per-process)
            backend.list_pairings()
        assert len(out) == 1
        assert out[0].pairing_id == "p-1"
        warnings = [
            r for r in caplog.records
            if "list_pairings" in r.getMessage()
        ]
        assert len(warnings) == 1

    def test_get_queue_falls_back(
        self, fresh_state_db: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        fallback = InMemoryBackend()
        fallback.seed_queue(
            "dedup",
            [QueueItem(
                stable_id="sid-001", kind="dedup",
                payload={"cluster": ["sid-001", "sid-001-dup"]},
            )],
            note="seeded",
        )
        backend = SqliteBackend(fresh_state_db, fallback=fallback)
        with caplog.at_level(logging.WARNING, logger=sb_mod.log.name):
            items, note = backend.get_queue("dedup")
        assert len(items) == 1
        assert note == "seeded"
        assert any(
            "get_queue" in r.getMessage() for r in caplog.records
        )

    def test_missing_tracks_table_falls_back(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Build a DB that has NO tracks table. Use raw sqlite3 so we can
        # intentionally omit migrations.
        db_path = tmp_path / "nostate.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE only_noise (x INTEGER)")
        conn.close()
        fallback = InMemoryBackend()
        now = _iso_now()
        fallback.seed_track(Track(
            stable_id="fb-1", title="Fallback Track", bpm=100.0,
            created_at=now, updated_at=now,
        ))
        backend = SqliteBackend(db_path, fallback=fallback)
        with caplog.at_level(logging.WARNING, logger=sb_mod.log.name):
            page = backend.list_tracks(TrackFilter())
            got = backend.get_track("fb-1")
            pls = backend.list_playlists()
        assert [t.stable_id for t in page.items] == ["fb-1"]
        assert got.title == "Fallback Track"
        assert pls == []
        msgs = [r.getMessage() for r in caplog.records]
        assert any("list_tracks" in m for m in msgs)
        assert any("get_track" in m for m in msgs)
        assert any("list_playlists" in m for m in msgs)

    def test_missing_playlists_table_falls_back_for_get(
        self, tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "nopl.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE other (x INTEGER)")
        conn.close()
        fallback = InMemoryBackend()
        backend = SqliteBackend(db_path, fallback=fallback)
        with pytest.raises(NotFoundError):
            backend.get_playlist("anything")

    def test_stats_falls_back_when_tables_missing(
        self, tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE x (y INTEGER)")
        conn.close()
        fallback = InMemoryBackend()
        backend = SqliteBackend(db_path, fallback=fallback)
        assert backend.stats() == {
            "tracks": 0, "playlists": 0, "pairings": 0,
        }

    def test_update_track_seeds_fallback_from_sqlite(
        self, fresh_state_db: Path,
    ) -> None:
        from apps.webui.server.etag import compute_etag
        backend = SqliteBackend(fresh_state_db)
        current = backend.get_track("sid-001")
        etag = compute_etag(current.stable_id, current.updated_at)
        updated = backend.update_track(
            "sid-001", {"notes": "updated from webui"},
            expected_etag=etag, source="webui",
        )
        assert updated.notes == "updated from webui"

    def test_create_and_delete_pairing_via_fallback(
        self, fresh_state_db: Path,
    ) -> None:
        from apps.webui.server.etag import compute_etag
        backend = SqliteBackend(fresh_state_db)
        now = _iso_now()
        p = backend.create_pairing(Pairing(
            pairing_id="p-a", from_stable_id="sid-001",
            to_stable_id="sid-002", direction="->",
            source="manual", notes="test",
            created_at=now, updated_at=now,
        ))
        assert p.pairing_id == "p-a"
        etag = compute_etag(p.pairing_id, p.updated_at)
        backend.delete_pairing("p-a", expected_etag=etag)
        assert backend.list_pairings() == []

    def test_last_writer_delegates(
        self, fresh_state_db: Path,
    ) -> None:
        backend = SqliteBackend(fresh_state_db)
        assert backend.last_writer() is None


# --- factory -------------------------------------------------------------

class TestFactory:
    def test_make_backend_returns_sqlite_when_file_exists(
        self, fresh_state_db: Path,
    ) -> None:
        backend = make_backend(fresh_state_db)
        assert isinstance(backend, SqliteBackend)

    def test_make_backend_returns_inmemory_when_missing(
        self, tmp_path: Path,
    ) -> None:
        backend = make_backend(tmp_path / "does-not-exist.db")
        assert isinstance(backend, InMemoryBackend)

    def test_make_backend_returns_inmemory_when_path_is_dir(
        self, tmp_path: Path,
    ) -> None:
        backend = make_backend(tmp_path)  # dir, not file
        assert isinstance(backend, InMemoryBackend)

    def test_make_backend_default_path_uses_shared_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apps.shared import paths as shared_paths
        missing = tmp_path / "not-there.db"
        monkeypatch.setattr(shared_paths, "STATE_DB", missing)
        backend = make_backend()
        assert isinstance(backend, InMemoryBackend)


# --- EAV edge cases ------------------------------------------------------

class TestEavEdgeCases:
    def test_bad_artists_json_is_null_artist(
        self, tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "s.db"
        conn = state_db.open_rw(db_path)
        try:
            conn.execute(
                "INSERT INTO tracks(stable_id, stable_id_tier, title, "
                "  artists_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("x", "inferred", "Broken", "{not valid json", ISO, ISO),
            )
        finally:
            conn.close()
        backend = SqliteBackend(db_path)
        t = backend.get_track("x")
        assert t.artist is None

    def test_non_list_artists_json_scalar_string(
        self, tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "s.db"
        conn = state_db.open_rw(db_path)
        try:
            conn.execute(
                "INSERT INTO tracks(stable_id, stable_id_tier, title, "
                "  artists_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("x", "inferred", "Scalar",
                 json.dumps("Solo Artist"), ISO, ISO),
            )
        finally:
            conn.close()
        backend = SqliteBackend(db_path)
        t = backend.get_track("x")
        assert t.artist == "Solo Artist"
