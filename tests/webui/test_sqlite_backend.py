"""Tests for :mod:`apps.webui.server.sqlite_backend`.

Covers:
  * round-trip reads against a freshly-initialized Phase 5 ``state.db``
    (tracks, playlists, playlist memberships, track_fields projection);
  * fallback paths for tables Phase 5 does not ship yet (``pairings``,
    queues) and the once-per-process warning cache;
  * :func:`make_backend` file-exists gating and its boot-time migration
    (issue #762: an existing state.db must be migrated before it is served,
    fail-fast if migration fails, never silently downgrade to InMemory).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.webui.server import sqlite_backend as sb_mod
from apps.webui.server.backend import (
    InMemoryBackend,
    MyTagScopeConflictError,
    NotFoundError,
    Pairing,
    QueueItem,
    Track,
    TrackFilter,
    compute_mytag_catalog_revision,
)
from apps.webui.server.sqlite_backend import (
    SqliteBackend,
    StaleStateSchemaError,
    make_backend,
)
from tests.test_schema_time_travel import _verified_v5_sql

pytestmark = [pytest.mark.requirement("CAT-05"), pytest.mark.rb_parity]


ISO = "2026-04-17T10:00:00.000000Z"


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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

    def test_list_tracks_filters_across_keyset_chunks(
        self, fresh_state_db: Path,
    ) -> None:
        conn = sqlite3.connect(fresh_state_db)
        try:
            conn.execute(
                "UPDATE track_fields SET value_json = ? "
                "WHERE stable_id = ? AND field_name = 'bpm'",
                (json.dumps(100.0), "sid-002"),
            )
            conn.execute(
                "INSERT INTO track_fields(stable_id, field_name, value_json, "
                "source, confidence, modified_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("sid-003", "bpm", json.dumps(130.0), "rekordbox", 1.0, ISO),
            )
            conn.commit()
        finally:
            conn.close()
        backend = SqliteBackend(fresh_state_db)
        first = backend.list_tracks(TrackFilter(bpm_min=120.0, limit=1))
        second = backend.list_tracks(
            TrackFilter(bpm_min=120.0, limit=1, cursor=first.next_cursor),
        )
        tail = backend.list_tracks(
            TrackFilter(bpm_min=120.0, limit=1, cursor=second.next_cursor),
        )

        assert [track.stable_id for track in first.items] == ["sid-001"]
        assert first.next_cursor == "sid-001"
        assert [track.stable_id for track in second.items] == ["sid-003"]
        assert second.next_cursor == "sid-003"
        assert tail.items == []
        assert tail.next_cursor is None

    def test_list_tracks_query_is_limited_to_the_requested_keyset_chunk(
        self, fresh_state_db: Path,
    ) -> None:
        statements: list[str] = []

        class TracedBackend(SqliteBackend):
            @contextmanager
            def _ro(self) -> Iterator[sqlite3.Connection]:
                with super()._ro() as conn:
                    conn.set_trace_callback(statements.append)
                    yield conn

        page = TracedBackend(fresh_state_db).list_tracks(TrackFilter(limit=2))
        track_selects = [
            statement for statement in statements
            if "FROM tracks WHERE deleted_at IS NULL" in statement
        ]

        assert [track.stable_id for track in page.items] == [
            "sid-001", "sid-002",
        ]
        assert track_selects == [
            "SELECT stable_id, title, artists_json, album, "
            "       duration_ms, file_path, created_at, updated_at "
            "FROM tracks WHERE deleted_at IS NULL ORDER BY stable_id LIMIT 2",
        ]

    def test_list_tracks_rejects_nan_numeric_fields(
        self, fresh_state_db: Path,
    ) -> None:
        conn = sqlite3.connect(fresh_state_db)
        try:
            conn.execute(
                "UPDATE track_fields SET value_json = ? "
                "WHERE stable_id = ? AND field_name = ?",
                ("NaN", "sid-002", "bpm"),
            )
            conn.execute(
                "INSERT INTO track_fields(stable_id, field_name, value_json, "
                "source, confidence, modified_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("sid-002", "rating", "NaN", "manual", 1.0, ISO),
            )
            conn.commit()
        finally:
            conn.close()
        backend = SqliteBackend(fresh_state_db)

        assert [track.stable_id for track in backend.list_tracks(
            TrackFilter(bpm_min=120.0),
        ).items] == ["sid-001"]
        assert [track.stable_id for track in backend.list_tracks(
            TrackFilter(rating_min=0),
        ).items] == ["sid-001"]

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

    def test_get_file_paths_bulk_matches_tracks_bulk_file_paths(
        self, fresh_state_db: Path,
    ) -> None:
        """pin e0f3a90652a9: the narrow read must agree with the general
        one on the one field they share, dedupe repeats, and omit ids that
        do not exist -- same absence contract as get_tracks_bulk."""
        backend = SqliteBackend(fresh_state_db)
        ids = ["sid-001", "sid-002", "sid-001", "missing-id"]

        narrow = backend.get_file_paths_bulk(ids)
        full = backend.get_tracks_bulk(ids)

        assert narrow == {
            sid: t.file_path for sid, t in full.items()
        }
        assert narrow == {
            "sid-001": "/music/midnight.mp3",
            "sid-002": "/music/oxide.mp3",
        }
        assert "missing-id" not in narrow

    def test_get_file_paths_bulk_empty_input(self, fresh_state_db: Path) -> None:
        backend = SqliteBackend(fresh_state_db)
        assert backend.get_file_paths_bulk([]) == {}

    def test_get_file_paths_bulk_never_invokes_the_eav_fetch(
        self, fresh_state_db: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pin e0f3a90652a9, review thread PRRT_kwDOSEvNd86fkJ-n: the
        InMemoryBackend/get_tracks_bulk-monkeypatch guards in
        tests/webui/test_playlists.py cannot detect a regression back to
        full-Track hydration on SqliteBackend, because InMemoryBackend has
        no EAV fetch to begin with. Exercise SqliteBackend directly and make
        ``_fetch_fields`` (the ``track_fields`` EAV pass -- see module
        docstring) raise if called at all, so this test can only pass while
        get_file_paths_bulk's SQL stays narrowed to ``stable_id, file_path``.

        Mutation-proved: reverting get_file_paths_bulk to route through
        get_tracks_bulk (which calls _fetch_fields for every row) makes this
        raise AssertionError from fail_if_called, confirmed locally before
        this test was pushed -- sid-001 alone carries 5 EAV rows in
        fresh_state_db, so the regression is not a near-miss."""
        def fail_if_called(*args: object, **kwargs: object) -> dict[str, dict]:
            raise AssertionError(
                "get_file_paths_bulk must never invoke _fetch_fields -- "
                "its whole point is skipping the EAV pass get_tracks_bulk pays"
            )

        monkeypatch.setattr(sb_mod, "_fetch_fields", fail_if_called)

        backend = SqliteBackend(fresh_state_db)
        result = backend.get_file_paths_bulk(
            ["sid-001", "sid-002", "sid-003", "missing-id"]
        )

        assert result == {
            "sid-001": "/music/midnight.mp3",
            "sid-002": "/music/oxide.mp3",
            "sid-003": "/music/gulf.flac",
        }


# --- fallbacks for Phase-5-missing entities ------------------------------

class TestFallbackPaths:
    def test_list_pairings_reads_sqlite_not_fallback(
        self, fresh_state_db: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        fallback = InMemoryBackend()
        now = _iso_now()
        fallback.seed_pairing(Pairing(
            pairing_id="p-fallback-only", from_stable_id="a", to_stable_id="b",
            direction="->", source="manual", notes=None,
            created_at=now, updated_at=now,
        ))
        backend = SqliteBackend(fresh_state_db, fallback=fallback)
        with caplog.at_level(logging.WARNING, logger=sb_mod.log.name):
            assert backend.list_pairings() == []
            backend.list_pairings()
        assert not any("list_pairings" in r.getMessage() for r in caplog.records)

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

    def test_missing_tracks_table_falls_back_for_file_paths_bulk(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        db_path = tmp_path / "nostate.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE only_noise (x INTEGER)")
        conn.close()
        fallback = InMemoryBackend()
        now = _iso_now()
        fallback.seed_track(Track(
            stable_id="fb-1", file_path="/music/fb-1.mp3",
            created_at=now, updated_at=now,
        ))
        backend = SqliteBackend(db_path, fallback=fallback)
        with caplog.at_level(logging.WARNING, logger=sb_mod.log.name):
            got = backend.get_file_paths_bulk(["fb-1"])
        assert got == {"fb-1": "/music/fb-1.mp3"}
        msgs = [r.getMessage() for r in caplog.records]
        assert any("get_file_paths_bulk" in m for m in msgs)

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

    def test_update_track_writes_through_state_writer(
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
        # Real persistence: sqlite (not the fallback) carries the edit.
        assert backend.get_track("sid-001").notes == "updated from webui"

    def test_update_track_genre_persists_across_reopen(
        self, fresh_state_db: Path,
    ) -> None:
        from apps.webui.server.etag import compute_etag

        backend = SqliteBackend(fresh_state_db)
        current = backend.get_track("sid-001")
        etag = compute_etag(current.stable_id, current.updated_at)
        backend.update_track(
            "sid-001", {"genre": "Breaks"},
            expected_etag=etag, source="webui",
        )
        reopened = SqliteBackend(fresh_state_db)
        track = reopened.get_track("sid-001")
        assert track.genre == "Breaks"
        assert track.provenance["genre"].source == "webui"

    def test_update_track_comments_persists_across_reopen(
        self, fresh_state_db: Path,
    ) -> None:
        from apps.webui.server.etag import compute_etag

        backend = SqliteBackend(fresh_state_db)
        current = backend.get_track("sid-001")
        etag = compute_etag(current.stable_id, current.updated_at)
        backend.update_track(
            "sid-001", {"comments": "late-night set"},
            expected_etag=etag, source="webui",
        )
        reopened = SqliteBackend(fresh_state_db)
        track = reopened.get_track("sid-001")
        assert track.comments == "late-night set"
        assert track.provenance["comments"].source == "webui"

    def test_update_track_tempo_pref_round_trips_and_defaults_null(
        self, fresh_state_db: Path,
    ) -> None:
        """PREF-01: unset is a real null, not a fabricated {regular: None, ...}."""
        from apps.webui.server.etag import compute_etag

        backend = SqliteBackend(fresh_state_db)
        before = backend.get_track("sid-001")
        assert before.tempo_pref is None
        etag = compute_etag(before.stable_id, before.updated_at)
        updated = backend.update_track(
            "sid-001",
            {"tempo_pref": {"regular": 140.0, "min": 138.0, "max": 142.0}},
            expected_etag=etag, source="webui",
        )
        assert updated.tempo_pref == {"regular": 140.0, "min": 138.0, "max": 142.0}
        # Real persistence, not the InMemory fallback.
        assert backend.get_track("sid-001").tempo_pref == {
            "regular": 140.0, "min": 138.0, "max": 142.0,
        }

    def test_update_track_tempo_pref_min_gte_max_rejected(
        self, fresh_state_db: Path,
    ) -> None:
        from apps.webui.server.backend import BackendError
        from apps.webui.server.etag import compute_etag

        backend = SqliteBackend(fresh_state_db)
        before = backend.get_track("sid-001")
        etag = compute_etag(before.stable_id, before.updated_at)
        with pytest.raises(BackendError):
            backend.update_track(
                "sid-001",
                {"tempo_pref": {"regular": 140.0, "min": 145.0, "max": 145.0}},
                expected_etag=etag, source="webui",
            )
        # Nothing persisted: the reject happens before any write.
        assert backend.get_track("sid-001").tempo_pref is None

    def test_update_track_tempo_pref_clamps_regular_into_new_range(
        self, fresh_state_db: Path,
    ) -> None:
        """A regular tempo now outside a newly-set range is clamped into it,
        never left out of bounds (PREF-01 trap case)."""
        from apps.webui.server.etag import compute_etag

        backend = SqliteBackend(fresh_state_db)
        etag = compute_etag("sid-001", backend.get_track("sid-001").updated_at)
        below = backend.update_track(
            "sid-001",
            {"tempo_pref": {"regular": 130.0, "min": 138.0, "max": 142.0}},
            expected_etag=etag, source="webui",
        )
        assert below.tempo_pref == {"regular": 138.0, "min": 138.0, "max": 142.0}

        etag2 = compute_etag("sid-001", below.updated_at)
        above = backend.update_track(
            "sid-001",
            {"tempo_pref": {"regular": 150.0, "min": 138.0, "max": 142.0}},
            expected_etag=etag2, source="webui",
        )
        assert above.tempo_pref == {"regular": 142.0, "min": 138.0, "max": 142.0}

    def test_update_track_tempo_pref_clear_to_null(
        self, fresh_state_db: Path,
    ) -> None:
        from apps.webui.server.etag import compute_etag

        backend = SqliteBackend(fresh_state_db)
        etag = compute_etag("sid-001", backend.get_track("sid-001").updated_at)
        set_ = backend.update_track(
            "sid-001",
            {"tempo_pref": {"regular": 140.0, "min": None, "max": None}},
            expected_etag=etag, source="webui",
        )
        assert set_.tempo_pref == {"regular": 140.0, "min": None, "max": None}
        etag2 = compute_etag("sid-001", set_.updated_at)
        cleared = backend.update_track(
            "sid-001", {"tempo_pref": None},
            expected_etag=etag2, source="webui",
        )
        assert cleared.tempo_pref is None

    def test_update_track_writes_file_path_via_upsert(
        self, fresh_state_db: Path,
    ) -> None:
        """relocate-files: file_path is a flat ``tracks`` column, not EAV,
        so it must round-trip through ``StateWriter.upsert_track`` -- the
        other flat columns (title/artists/album/isrc/duration_ms) must
        survive unchanged."""
        from apps.webui.server.etag import compute_etag

        backend = SqliteBackend(fresh_state_db)
        before = backend.get_track("sid-001")
        etag = compute_etag(before.stable_id, before.updated_at)
        updated = backend.update_track(
            "sid-001", {"file_path": "/music/relocated/midnight.mp3"},
            expected_etag=etag, source="webui",
        )
        assert updated.file_path == "/music/relocated/midnight.mp3"
        assert updated.title == before.title
        assert updated.artist == before.artist
        assert updated.album == before.album
        assert updated.duration_ms == before.duration_ms
        # Real persistence: a fresh read reflects the same change.
        reread = backend.get_track("sid-001")
        assert reread.file_path == "/music/relocated/midnight.mp3"

    def test_update_track_post_mutation_guard_failure_rolls_back_transaction(
        self, fresh_state_db: Path,
    ) -> None:
        """A pathname mismatch after upsert must roll back before COMMIT."""
        from apps.webui.server.etag import compute_etag

        backend = SqliteBackend(fresh_state_db)
        before = backend.get_track("sid-001")
        etag = compute_etag(before.stable_id, before.updated_at)
        checks = 0

        def _fail_postcheck() -> None:
            nonlocal checks
            checks += 1
            if checks == 2:
                raise RuntimeError("candidate pathname changed")

        with pytest.raises(RuntimeError, match="candidate pathname changed"):
            backend.update_track(
                "sid-001", {"file_path": "/music/relocated/midnight.mp3"},
                expected_etag=etag, source="webui", mutation_guard=_fail_postcheck,
            )
        assert checks == 2
        assert backend.get_track("sid-001").file_path == before.file_path

    def test_update_track_concurrent_updates_no_lost_update(
        self, fresh_state_db: Path,
    ) -> None:
        """Adversarial R4 finding (TOCTOU on update_track).

        Two threads acquire the same ``expected_etag`` and call
        ``update_track`` concurrently. The write lock serialises the
        read-CAS-write sequence, and the winner's ``track_fields``
        stamp advances the effective ``updated_at``, so the loser's
        CAS must fail with ConflictError -- exactly one update
        succeeds and the durable sqlite state reflects the winner.
        """
        import threading

        from apps.webui.server.backend import ConflictError
        from apps.webui.server.etag import compute_etag

        backend = SqliteBackend(fresh_state_db)
        current = backend.get_track("sid-001")
        shared_etag = compute_etag(current.stable_id, current.updated_at)

        # Barrier to maximise the interleaving window: both threads
        # hit ``update_track`` together, so whoever loses the race
        # must be rejected by the lock + CAS rather than silently
        # overwriting the winner.
        start = threading.Barrier(2)
        results: dict = {}

        def _worker(tag: str, note: str) -> None:
            start.wait()
            try:
                out = backend.update_track(
                    "sid-001", {"notes": note},
                    expected_etag=shared_etag, source="webui",
                )
                results[tag] = ("ok", out.notes)
            except ConflictError as exc:
                results[tag] = ("conflict", exc)

        t1 = threading.Thread(target=_worker, args=("a", "note-A"))
        t2 = threading.Thread(target=_worker, args=("b", "note-B"))
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        outcomes = sorted(v[0] for v in results.values())
        assert outcomes == ["conflict", "ok"], (
            f"expected exactly one ok + one conflict, got {results}"
        )

        # Final durable state must reflect the winner, not be silently
        # overwritten by the loser.
        winner_note = next(
            v[1] for v in results.values() if v[0] == "ok"
        )
        final = backend.get_track("sid-001")
        assert final.notes == winner_note
        assert final.provenance["notes"].source == "webui"

    def test_update_tracks_rolls_back_every_row_when_a_later_write_fails(
        self, fresh_state_db: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If one StateWriter call fails, an earlier row must not persist."""
        from apps.webui.server.backend import TrackUpdate
        from apps.webui.server.etag import compute_etag

        backend = SqliteBackend(fresh_state_db)
        first = backend.get_track("sid-001")
        second = backend.get_track("sid-002")
        original_set_field = sb_mod.StateWriter.set_field
        calls = 0

        def fail_second_write(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected second write failure")
            return original_set_field(self, *args, **kwargs)

        monkeypatch.setattr(sb_mod.StateWriter, "set_field", fail_second_write)
        with pytest.raises(RuntimeError, match="injected second write failure"):
            backend.update_tracks([
                TrackUpdate("sid-001", {"notes": "first changed"}, compute_etag(first.stable_id, first.updated_at)),
                TrackUpdate("sid-002", {"notes": "second changed"}, compute_etag(second.stable_id, second.updated_at)),
            ])

        assert backend.get_track("sid-001").notes == first.notes
        assert backend.get_track("sid-002").notes == second.notes

    def test_update_tag_members_rejects_a_member_added_before_transaction_lock(
        self, fresh_state_db: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The transaction validates the observed scope after its writer lock."""
        backend = SqliteBackend(fresh_state_db)
        expected_catalog_revision = compute_mytag_catalog_revision(
            backend.list_tracks(TrackFilter(limit=1000)).items,
        )
        original_open_rw = sb_mod._state_db.open_rw
        injected = False

        def open_rw_after_racing_add(*args, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                race_conn = original_open_rw(*args, **kwargs)
                try:
                    with sb_mod.StateWriter(race_conn, actor="test-racer") as writer:
                        writer.set_field(
                            "sid-002", "tags", ["late"], source="webui",
                            modified_at=_iso_now(), confidence=1.0,
                        )
                finally:
                    race_conn.close()
            return original_open_rw(*args, **kwargs)

        monkeypatch.setattr(sb_mod._state_db, "open_rw", open_rw_after_racing_add)
        with pytest.raises(MyTagScopeConflictError) as exc_info:
            backend.update_tag_members(
                "late", "renamed",
                expected_catalog_revision=expected_catalog_revision,
                expected_track_count=0,
            )
        assert exc_info.value.affected_track_count == 1
        assert backend.get_track("sid-002").tags == ["late"]
        assert backend.get_track("sid-001").tags == ["deep-house", "smooth"]

    def test_create_and_delete_pairing_persists_in_sqlite(
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
        listed = backend.list_pairings()
        assert len(listed) == 1
        assert listed[0].notes == "test"
        etag = compute_etag(p.pairing_id, p.updated_at)
        backend.delete_pairing("p-a", expected_etag=etag)
        assert backend.list_pairings() == []
        backend2 = SqliteBackend(fresh_state_db)
        assert backend2.list_pairings() == []

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

    def test_make_backend_migrates_a_stale_existing_db(
        self, tmp_path: Path,
    ) -> None:
        """issue #762: an unmigrated (v5) db must be upgraded before serving.

        Restores the pinned v5 dump (the same fixture used by
        ``tests/test_schema_time_travel.py``), seeds one row in the OLD
        shape, then goes through ``make_backend`` -- the real boot-time
        factory, not ``apply_migrations`` directly -- and proves the
        resulting backend serves a v7-shaped query (``deleted_at``,
        added in v7) instead of ``sqlite3.OperationalError``.
        """
        db_path = tmp_path / "state.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(_verified_v5_sql())
            conn.execute(
                "INSERT INTO tracks(stable_id, stable_id_tier, title, "
                "  file_path, created_at, updated_at) "
                "VALUES ('sid-v5', 'inferred', 'Old Build Track', "
                "  '/music/old.mp3', ?, ?)",
                (ISO, ISO),
            )
            conn.commit()
        finally:
            conn.close()

        backend = make_backend(db_path)

        assert isinstance(backend, SqliteBackend)
        # stats() runs a WHERE deleted_at IS NULL-shaped query (v7); this
        # crashes with "no such column: deleted_at" on an unmigrated v5 db.
        assert backend.stats()["tracks"] == 1

        verify_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            version = verify_conn.execute(
                "SELECT MAX(version) FROM schema_meta"
            ).fetchone()[0]
        finally:
            verify_conn.close()
        assert version == state_schema.SCHEMA_VERSION

    def test_make_backend_refuses_tracks_without_schema_meta(
        self, tmp_path: Path,
    ) -> None:
        """Issue #790: a foreign old tracks table must fail before migration.

        ``MIGRATIONS[0]`` leaves an existing table in place, then its index
        creation needs columns this deliberately old shape does not have.
        The factory must surface the established remediation error instead of
        leaking that implementation-specific SQLite column failure.
        """
        db_path = tmp_path / "state.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "CREATE TABLE tracks (stable_id TEXT PRIMARY KEY, title TEXT)"
            )
            conn.execute(
                "INSERT INTO tracks VALUES ('legacy-1', 'Legacy Track')"
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(StaleStateSchemaError, match="version 0"):
            make_backend(db_path)

    def test_make_backend_raises_when_migration_cannot_write_and_leaves_version_unchanged(
        self, tmp_path: Path,
    ) -> None:
        """Negative control (issue #762 AC4, partial -- see caveat below): a
        real write failure on the migrate-or-die path must abort
        ``make_backend`` with the error surfaced -- never fall back to
        InMemoryBackend -- and the db must be left at its pre-failure
        version, proving ``open_rw``'s ``conn.close(); raise`` holds end to
        end through the real boot-time entry point.

        Caveat: this fires at ``open_rw``'s own ``PRAGMA journal_mode = WAL``,
        before ``apply_migrations`` runs a single statement -- not inside a
        migration step itself, as AC4's literal wording says. None of
        ``MIGRATIONS`` v6/v7 can be made to raise against real v5 data
        without fabricating broken SQL (repo policy, AGENTS.md): every
        ``ALTER TABLE ... ADD COLUMN`` there is nullable with no
        UNIQUE/FK/CHECK constraint touching pre-existing rows, and the new
        constrained tables (``users``, ``machines``, ...) aren't populated
        by the migration itself. So this proves the surrounding invariant
        (any real failure on this path aborts, never partially commits) but
        not literally "a migration step that raises"; that gap is open.

        No fabricated SQL or patched migration content: ``open_rw``'s WAL
        pragma needs to create ``<db>-wal`` next to the main file, so
        pre-occupying that exact path with a directory makes the OS refuse
        the open with a genuine ``sqlite3.OperationalError`` -- a real
        failure a deployed engine can hit (another process already holding
        that path, a leftover directory from a botched install). Unlike a
        read-only chmod, this is not a permission check a privileged (root)
        process can bypass: ``open()`` on a path that is a directory fails
        for every uid, so this stays a real negative control under any
        runner.
        """
        db_path = tmp_path / "state.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(_verified_v5_sql())
            conn.commit()
        finally:
            conn.close()

        pre_conn = sqlite3.connect(str(db_path))
        try:
            pre_version = pre_conn.execute(
                "SELECT MAX(version) FROM schema_meta"
            ).fetchone()[0]
        finally:
            pre_conn.close()
        assert pre_version == 5

        wal_path = db_path.parent / f"{db_path.name}-wal"
        wal_path.mkdir()
        with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
            make_backend(db_path)
        wal_path.rmdir()

        verify_conn = sqlite3.connect(str(db_path))
        try:
            version = verify_conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_meta"
            ).fetchone()[0]
        finally:
            verify_conn.close()
        assert version == pre_version, (
            "a real, OS-level write failure while opening for migration must "
            "not advance schema_meta; the db should still be sitting at its "
            "pre-failure version"
        )


class TestSchemaGuard:
    """SqliteBackend's own defense-in-depth guard (issue #762 AC2)."""

    def test_sqlite_backend_refuses_a_stale_tracks_schema(
        self, tmp_path: Path,
    ) -> None:
        """A real Phase 5 db (has ``tracks``) below SCHEMA_VERSION must be
        refused loudly at construction, even when a caller bypasses
        ``make_backend`` and constructs ``SqliteBackend`` directly."""
        db_path = tmp_path / "state.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(_verified_v5_sql())
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(StaleStateSchemaError, match="version 5"):
            SqliteBackend(db_path)


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
