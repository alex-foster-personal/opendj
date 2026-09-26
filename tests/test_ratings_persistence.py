"""Regression: webui PATCH edits persist across daemon restart.

Unit ratings-persistence-restart (ledger + RECON-BACKEND): rating / notes /
tags PATCHes on ``/tracks/{stable_id}`` must land in ``state.db``
``track_fields`` via the Phase 5 single-writer
(:class:`apps.shared.state.writer.StateWriter`) with provenance
(``source='webui'``, ``confidence=1.0``) so a NEW backend instance -- i.e.
a restarted daemon -- sees the edits.

Acceptance tests (each maps to one test below):

  [if] update_track sets rating/notes/tags and a NEW SqliteBackend
       instance does not return the same values [then broken]
  [if] the edit lands without track_fields rows carrying source='webui'
       + confidence=1.0 and a track.field.set event [then broken]
  [if] a stale If-Match etag is accepted after an edit [then broken]
  [if] an HTTP PATCH via the FastAPI app does not survive a backend
       swap (restart simulation) [then broken]
  [if] a v2-schema state.db rejects source='webui' after migration
       [then broken]
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.backend import ConflictError, Track
from apps.webui.server.etag import compute_etag
from apps.webui.server.sqlite_backend import SqliteBackend, make_backend

pytestmark = pytest.mark.requirement("CAT-05")

SID = "sid-persist-001"


@pytest.fixture
def state_db_path(tmp_path: Path) -> Path:
    """Fresh state.db seeded through the documented writer path."""
    db_path = tmp_path / "state.db"
    conn = state_db.open_rw(db_path)
    try:
        with StateWriter(conn) as writer:
            writer.upsert_track(
                stable_id=SID,
                stable_id_tier="isrc",
                title="Midnight Drive",
                artists=["the maintainer"],
                album="Drive",
                isrc="USAAA0000001",
                duration_ms=240_000,
                file_path="/music/midnight.mp3",
            )
    finally:
        conn.close()
    return db_path


def _served_etag(track: Track) -> str:
    """The etag the API serves for ``track``, selection variant included.

    Every serving path passes ``track.selection_tag`` (``routes/tracks.py``,
    ``track_rows.py``, the batch write guard). Since STANDALONE-06 (#3926) an
    unmapped track's lane-owned fields default to own, so its variant is not
    empty and the two-argument form no longer names the served validator.
    """
    return compute_etag(track.stable_id, track.updated_at, track.selection_tag)


def _patch_all_fields(backend: SqliteBackend) -> str:
    """Apply a rating+notes+tags patch; return the post-write etag."""
    current = backend.get_track(SID)
    etag = _served_etag(current)
    updated = backend.update_track(
        SID,
        {"rating": 5, "notes": "set from webui", "tags_add": ["peak-time"]},
        expected_etag=etag,
        source="webui",
    )
    return _served_etag(updated)


class TestRestartPersistence:
    def test_patch_survives_new_backend_instance(
        self, state_db_path: Path,
    ) -> None:
        first = SqliteBackend(state_db_path)
        _patch_all_fields(first)
        del first

        # Restart simulation: brand-new backend over the same file.
        reborn = SqliteBackend(state_db_path)
        track = reborn.get_track(SID)
        assert track.rating == 5
        assert track.notes == "set from webui"
        assert track.tags == ["peak-time"]
        for field_name in ("rating", "notes", "tags"):
            prov = track.provenance[field_name]
            assert prov.source == "webui", field_name
            assert prov.confidence == 1.0, field_name

    def test_provenance_rows_and_event_in_state_db(
        self, state_db_path: Path,
    ) -> None:
        backend = SqliteBackend(state_db_path)
        _patch_all_fields(backend)

        conn = sqlite3.connect(str(state_db_path))
        try:
            rows = {
                r[0]: r
                for r in conn.execute(
                    "SELECT field_name, value_json, source, confidence "
                    "FROM track_fields WHERE stable_id = ?",
                    (SID,),
                )
            }
            assert set(rows) == {"rating", "notes", "tags"}
            assert json.loads(rows["rating"][1]) == 5
            assert json.loads(rows["notes"][1]) == "set from webui"
            assert json.loads(rows["tags"][1]) == ["peak-time"]
            for field_name, row in rows.items():
                assert row[2] == "webui", field_name
                assert row[3] == 1.0, field_name
            events = conn.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE kind = 'track.field.set' AND stable_id = ?",
                (SID,),
            ).fetchone()[0]
            assert events == 3
        finally:
            conn.close()

    def test_etag_advances_and_stale_etag_conflicts(
        self, state_db_path: Path,
    ) -> None:
        backend = SqliteBackend(state_db_path)
        current = backend.get_track(SID)
        stale_etag = _served_etag(current)
        new_etag = _patch_all_fields(backend)
        assert new_etag != stale_etag

        with pytest.raises(ConflictError):
            backend.update_track(
                SID, {"rating": 1},
                expected_etag=stale_etag, source="webui",
            )

        # The fresh etag is accepted -- and it is re-derived identically
        # by a NEW backend instance (etag survives restart too).
        reborn = SqliteBackend(state_db_path)
        track = reborn.get_track(SID)
        assert _served_etag(track) == new_etag
        reborn.update_track(
            SID, {"rating": 3}, expected_etag=new_etag, source="webui",
        )
        assert reborn.get_track(SID).rating == 3


class TestHttpPatchRoundTrip:
    @pytest.fixture
    def client(self, state_db_path: Path) -> Iterator[TestClient]:
        app = create_app(
            backend=SqliteBackend(state_db_path),
            bind_host="127.0.0.1", hostname="test-host",
            lock_status_fn=lambda: None, syncthing_status_fn=lambda: None,
        )
        with TestClient(app) as c:
            yield c

    def test_http_patch_persists_across_restart(
        self, client: TestClient, state_db_path: Path,
    ) -> None:
        got = client.get(f"/api/v1/tracks/{SID}")
        assert got.status_code == 200
        etag = got.headers["ETag"]

        patched = client.patch(
            f"/api/v1/tracks/{SID}",
            json={"rating": 4, "notes": "http edit", "tags_add": ["warmup"]},
            headers={"If-Match": etag},
        )
        assert patched.status_code == 200, patched.text
        body = patched.json()
        assert body["rating"] == 4
        assert body["notes"] == "http edit"
        assert body["tags"] == ["warmup"]
        assert patched.headers["ETag"] != etag

        # Restart simulation at the backend layer.
        reborn = SqliteBackend(state_db_path)
        track = reborn.get_track(SID)
        assert track.rating == 4
        assert track.notes == "http edit"
        assert track.tags == ["warmup"]
        assert track.provenance["rating"].source == "webui"

    def test_http_patch_stale_etag_is_409(
        self, client: TestClient,
    ) -> None:
        got = client.get(f"/api/v1/tracks/{SID}")
        etag = got.headers["ETag"]
        first = client.patch(
            f"/api/v1/tracks/{SID}", json={"rating": 2},
            headers={"If-Match": etag},
        )
        assert first.status_code == 200
        second = client.patch(
            f"/api/v1/tracks/{SID}", json={"rating": 1},
            headers={"If-Match": etag},
        )
        assert second.status_code == 409


class TestSchemaMigration:
    def test_v2_db_migrates_and_accepts_webui_source(
        self, tmp_path: Path,
    ) -> None:
        """A pre-existing v2 state.db (source CHECK without 'webui') must
        migrate in place on boot and accept the edit.

        Routes through ``make_backend()``, the boot-path entrypoint that
        actually performs the migration (issue #762), rather than
        constructing ``SqliteBackend`` directly: since #762's fix,
        ``SqliteBackend.__init__`` refuses a stale (unmigrated) db outright
        (``StaleStateSchemaError``) as a defense-in-depth guard against any
        caller that skips ``make_backend()``, so direct construction is no
        longer the self-migrating path this test means to exercise.
        """
        from apps.shared.state import schema as state_schema

        db_path = tmp_path / "v2.db"
        conn = sqlite3.connect(str(db_path))
        try:
            # Build v1+v2 only (the pre-'webui' schema).
            conn.execute(
                "CREATE TABLE schema_meta ("
                "  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            for version, statements in enumerate(
                state_schema.MIGRATIONS[:2], start=1,
            ):
                for stmt in statements:
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_meta(version, applied_at) "
                    "VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat()),
                )
            now = datetime.now(UTC).isoformat()
            conn.execute(
                "INSERT INTO tracks(stable_id, stable_id_tier, title, "
                "  artists_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (SID, "isrc", "Legacy", json.dumps(["A"]), now, now),
            )
            conn.execute(
                "INSERT INTO track_fields(stable_id, field_name, value_json, "
                "  source, confidence, modified_at) "
                "VALUES (?, 'bpm', '124.0', 'rekordbox', 0.9, ?)",
                (SID, now),
            )
            # Pre-migration schema must reject 'webui' (sanity check).
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO track_fields(stable_id, field_name, "
                    "  value_json, source, confidence, modified_at) "
                    "VALUES (?, 'rating', '5', 'webui', 1.0, ?)",
                    (SID, now),
                )
            conn.commit()
        finally:
            conn.close()

        backend = make_backend(db_path)
        assert isinstance(backend, SqliteBackend)
        current = backend.get_track(SID)
        etag = _served_etag(current)
        updated = backend.update_track(
            SID, {"rating": 5}, expected_etag=etag, source="webui",
        )
        assert updated.rating == 5
        # Pre-existing provenance survived the table rebuild.
        assert updated.provenance["bpm"].source == "rekordbox"
