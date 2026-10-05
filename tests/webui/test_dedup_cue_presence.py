"""Cue/hotcue/loop/beatgrid presence on GET /dedup/clusters (issue #2454).

- [if] two duplicate tracks are compared and one has cue points the other lacks [then] the comparison UI shows this before a merge decision is made, [else stop].
- [if] a merge would discard the only copy with cue points [then] the user gets an explicit warning before confirming, [else stop].
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server import dedup_decisions
from apps.webui.server.backend import InMemoryBackend, Track
from apps.webui.server.dedup_cue_presence import bulk_cue_presence
from apps.webui.server.routes import dedup_review
from tests.webui.test_dedup_review import _seed_cluster_db

pytestmark = [pytest.mark.requirement("LIBMX-09"), pytest.mark.rb_parity]

CANON = "track-canon"
ALIAS = "track-alias"
CANON_VENDOR = "vendor-canon"
ALIAS_VENDOR = "vendor-alias"


def _make_master_plain_db(path: Path) -> None:
    """djmdContent/djmdGenre/djmdCue, matching the fuller schema in
    test_hardening_round2.py's own _make_master_plain_db.

    test_apply_still_rewrites_playlists_only (below) round-trips a real
    /apply request through SqliteBackend, whose rb metadata enrichment
    LEFT JOINs djmdContent to djmdGenre (apps/webui/server/rb_vendor_pkg/
    track_rows.py::bulk_rb_meta) -- unlike the other tests in this file,
    which read via InMemoryBackend or call bulk_cue_presence() directly and
    so never reach that join. A LEFT JOIN still requires the joined table
    to exist even with zero genre rows, so the fixture needs the full
    djmdContent column set the query selects, plus the djmdGenre table
    itself, not just the cue-only shape.

    This gap was harmless until cad91e688d (issue #3536, Fri 25 Sep 2026):
    before that commit, analysis_overlay.lane_owned_fields() skipped its
    rb-mapped probe (and so bulk_rb_meta and this join) whenever every lane
    was on the default "rbx" selection, which is what this fixture uses.
    cad91e688d made that probe unconditional (STANDALONE-02/06 needs the
    per-track rb-mapped flag even under an all-rbx selection), so every
    request through SqliteBackend now reaches bulk_rb_meta regardless of
    selection, and this test's incomplete fixture schema turned into a real
    failure the same day. Verified green pre-cad91e688d (CI run 36188349037
    at db12b7a1979) and red after.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE djmdContent (ID VARCHAR(255) PRIMARY KEY, "
            "FolderPath VARCHAR(255), ImagePath VARCHAR(255), "
            "AnalysisDataPath VARCHAR(255), Commnt VARCHAR(255), "
            "GenreID VARCHAR(255), DJPlayCount INTEGER, Length INTEGER, "
            "rb_local_deleted TINYINT(1) DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE djmdGenre (ID VARCHAR(255) PRIMARY KEY, "
            "Name VARCHAR(255), rb_local_deleted TINYINT(1) DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE djmdCue ("
            "ID VARCHAR(255) PRIMARY KEY, ContentID VARCHAR(255), "
            "InMsec INTEGER, InFrame INTEGER, InMpegFrame INTEGER, "
            "InMpegAbs INTEGER, OutMsec INTEGER, OutFrame INTEGER, "
            "Kind INTEGER, Color INTEGER, ColorTableIndex INTEGER, "
            "ActiveLoop INTEGER, Comment VARCHAR(255), BeatLoopSize INTEGER, "
            "rb_local_deleted TINYINT(1) DEFAULT 0, "
            "created_at DATETIME, updated_at DATETIME)"
        )
        conn.execute(
            "INSERT INTO djmdContent (ID, Length, AnalysisDataPath) VALUES (?, ?, ?)",
            (CANON_VENDOR, 300, ""),
        )
        conn.execute(
            "INSERT INTO djmdContent (ID, Length, AnalysisDataPath) VALUES (?, ?, ?)",
            (ALIAS_VENDOR, 300, ""),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_state_db(
    path: Path,
    *,
    canon_vendor: str = CANON_VENDOR,
    alias_vendor: str = ALIAS_VENDOR,
    beatgrid_sid: str | None = None,
) -> None:
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        writer.upsert_track(
            stable_id=CANON,
            stable_id_tier="inferred",
            title="Midnight Drive",
            artists=["Tamsin Quell"],
            album=None,
            isrc=None,
            duration_ms=210_000,
            file_path="/music/canon.flac",
        )
        writer.upsert_track(
            stable_id=ALIAS,
            stable_id_tier="inferred",
            title="Midnight Drive (128k)",
            artists=["Tamsin Quell"],
            album=None,
            isrc=None,
            duration_ms=210_000,
            file_path="/music/alias-128.mp3",
        )
        conn.execute(
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES (?, 'rekordbox', ?)",
            (CANON, canon_vendor),
        )
        conn.execute(
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES (?, 'rekordbox', ?)",
            (ALIAS, alias_vendor),
        )
        if beatgrid_sid is not None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS analysis_canonical (
                    stable_id TEXT, lane TEXT, backend TEXT, backend_version TEXT,
                    updated_at TEXT, PRIMARY KEY (stable_id, lane)
                )
                """
            )
            conn.execute(
                "INSERT INTO analysis_canonical (stable_id, lane, backend, backend_version, updated_at) "
                "VALUES (?, 'beatgrid', 'test', '1', '2026-01-01T00:00:00Z')",
                (beatgrid_sid,),
            )
        conn.commit()
    finally:
        writer.close()
        conn.close()


def _insert_cue(
    master_path: Path,
    *,
    cue_id: str,
    content_id: str,
    kind: int,
    in_msec: int | None,
    out_msec: int | None = None,
    deleted: int = 0,
) -> None:
    conn = sqlite3.connect(str(master_path))
    try:
        conn.execute(
            "INSERT INTO djmdCue (ID, ContentID, InMsec, OutMsec, Kind, rb_local_deleted) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (cue_id, content_id, in_msec, out_msec, kind, deleted),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def seeded_backend() -> InMemoryBackend:
    backend = InMemoryBackend()
    backend.seed_track(
        Track(
            stable_id=CANON,
            title="Midnight Drive",
            artist="Tamsin Quell",
            bpm=124.0,
            key="8A",
            duration_ms=210_000,
            rating=4,
        )
    )
    backend.seed_track(
        Track(
            stable_id=ALIAS,
            title="Midnight Drive (128k)",
            artist="Tamsin Quell",
            bpm=124.0,
            key="8A",
            duration_ms=210_000,
            rating=None,
        )
    )
    return backend


@pytest.fixture
def dedup_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "phase7.sqlite"
    decisions_path = tmp_path / "review-decisions.json"
    monkeypatch.setattr(dedup_review.dedup_paths, "DEDUP_FALLBACK_DB", db_path)
    monkeypatch.setattr(dedup_review, "DECISIONS_FILE", decisions_path)
    monkeypatch.setattr(dedup_decisions, "DECISIONS_FILE", decisions_path)
    return db_path


@pytest.fixture
def cue_fixture(
    tmp_path: Path,
    dedup_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    state_path = tmp_path / "state.db"
    master_path = tmp_path / "master.plain.db"
    _make_master_plain_db(master_path)
    _seed_state_db(state_path)
    _insert_cue(master_path, cue_id="cue-1", content_id=ALIAS_VENDOR, kind=1, in_msec=30_000)
    _insert_cue(master_path, cue_id="cue-2", content_id=ALIAS_VENDOR, kind=1, in_msec=90_000)
    _seed_cluster_db(
        dedup_db,
        cluster_id=1,
        canonical_sid=CANON,
        canonical_path="/music/canon.flac",
        alias_sid=ALIAS,
        alias_path="/music/alias-128.mp3",
    )
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master_path)
    return state_path, master_path


@pytest.fixture
def app_client(seeded_backend: InMemoryBackend, cue_fixture: tuple[Path, Path]):
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app

    app = create_app(
        backend=seeded_backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def app_client_no_cue_db(
    seeded_backend: InMemoryBackend, dedup_db: Path,
) -> Iterator:
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app

    _seed_cluster_db(
        dedup_db,
        cluster_id=1,
        canonical_sid=CANON,
        canonical_path="/music/canon.flac",
        alias_sid=ALIAS,
        alias_path="/music/alias-128.mp3",
    )
    app = create_app(
        backend=seeded_backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
    )
    with TestClient(app) as client:
        yield client


def _member_by_sid(cluster: dict, stable_id: str) -> dict:
    return next(member for member in cluster["members"] if member["stable_id"] == stable_id)


# REQ: LIBM-76
def test_clusters_show_asymmetric_cue_presence(app_client, dedup_db: Path) -> None:
    response = app_client.get("/api/v1/dedup/clusters")
    assert response.status_code == 200
    cluster = response.json()["clusters"][0]
    assert cluster["decision"] is None
    alias = _member_by_sid(cluster, ALIAS)
    canon = _member_by_sid(cluster, CANON)
    assert alias["cue_count"] == 2
    assert alias["hot_cue_count"] == 2
    assert alias["loop_count"] == 0
    assert alias["cue_positions_ms"] == [30_000, 90_000]
    assert canon["cue_count"] == 0
    assert canon["hot_cue_count"] == 0
    assert canon["loop_count"] == 0
    assert canon["cue_positions_ms"] == []


def test_memory_loop_counts_as_loop_not_hot_cue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    master_path = tmp_path / "master.plain.db"
    _make_master_plain_db(master_path)
    _insert_cue(
        master_path,
        cue_id="loop-0",
        content_id=CANON_VENDOR,
        kind=0,
        in_msec=10_000,
        out_msec=20_000,
    )
    state_path = tmp_path / "state.db"
    _seed_state_db(state_path)
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master_path)
    presence = bulk_cue_presence([CANON])[CANON]
    assert presence.cue_count == 1
    assert presence.hot_cue_count == 0
    assert presence.loop_count == 1


def test_hot_cue_loop_counts_in_both_hot_and_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    master_path = tmp_path / "master.plain.db"
    _make_master_plain_db(master_path)
    _insert_cue(
        master_path,
        cue_id="loop-2",
        content_id=CANON_VENDOR,
        kind=2,
        in_msec=15_000,
        out_msec=25_000,
    )
    state_path = tmp_path / "state.db"
    _seed_state_db(state_path)
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master_path)
    presence = bulk_cue_presence([CANON])[CANON]
    assert presence.cue_count == 1
    assert presence.hot_cue_count == 1
    assert presence.loop_count == 1


def test_kind_nine_excluded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    master_path = tmp_path / "master.plain.db"
    _make_master_plain_db(master_path)
    _insert_cue(
        master_path,
        cue_id="kind-9",
        content_id=CANON_VENDOR,
        kind=9,
        in_msec=5_000,
    )
    state_path = tmp_path / "state.db"
    _seed_state_db(state_path)
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master_path)
    result = bulk_cue_presence([CANON])
    assert CANON not in result or result[CANON].cue_count == 0


def test_deleted_cue_excluded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    master_path = tmp_path / "master.plain.db"
    _make_master_plain_db(master_path)
    _insert_cue(
        master_path,
        cue_id="deleted",
        content_id=CANON_VENDOR,
        kind=1,
        in_msec=5_000,
        deleted=1,
    )
    state_path = tmp_path / "state.db"
    _seed_state_db(state_path)
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master_path)
    result = bulk_cue_presence([CANON])
    assert CANON not in result or result[CANON].cue_count == 0


def test_missing_dbs_return_empty_fields_not_500(
    app_client_no_cue_db, monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = Path("/nonexistent/state.db")
    monkeypatch.setattr(rb_config, "STATE_DB", missing)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", Path("/nonexistent/master.plain.db"))
    response = app_client_no_cue_db.get("/api/v1/dedup/clusters")
    assert response.status_code == 200
    for member in response.json()["clusters"][0]["members"]:
        assert member["cue_count"] == 0
        assert member["hot_cue_count"] == 0
        assert member["loop_count"] == 0
        assert member["has_beatgrid"] is False
        assert member["cue_positions_ms"] == []


def test_beatgrid_from_analysis_data_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dedup_db: Path, seeded_backend: InMemoryBackend,
) -> None:
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app

    state_path = tmp_path / "state.db"
    master_path = tmp_path / "master.plain.db"
    _make_master_plain_db(master_path)
    _seed_state_db(state_path)
    conn = sqlite3.connect(str(master_path))
    try:
        conn.execute(
            "UPDATE djmdContent SET AnalysisDataPath = ? WHERE ID = ?",
            ("/tmp/ANLZ0000", ALIAS_VENDOR),
        )
        conn.commit()
    finally:
        conn.close()
    _seed_cluster_db(
        dedup_db,
        cluster_id=1,
        canonical_sid=CANON,
        canonical_path="/music/canon.flac",
        alias_sid=ALIAS,
        alias_path="/music/alias-128.mp3",
    )
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master_path)
    app = create_app(
        backend=seeded_backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
    )
    with TestClient(app) as client:
        cluster = client.get("/api/v1/dedup/clusters").json()["clusters"][0]
    assert _member_by_sid(cluster, ALIAS)["has_beatgrid"] is True
    assert _member_by_sid(cluster, CANON)["has_beatgrid"] is False


def test_beatgrid_from_analysis_canonical_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dedup_db: Path, seeded_backend: InMemoryBackend,
) -> None:
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app

    state_path = tmp_path / "state.db"
    master_path = tmp_path / "master.plain.db"
    _make_master_plain_db(master_path)
    _seed_state_db(state_path, beatgrid_sid=ALIAS)
    _seed_cluster_db(
        dedup_db,
        cluster_id=1,
        canonical_sid=CANON,
        canonical_path="/music/canon.flac",
        alias_sid=ALIAS,
        alias_path="/music/alias-128.mp3",
    )
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master_path)
    app = create_app(
        backend=seeded_backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
    )
    with TestClient(app) as client:
        cluster = client.get("/api/v1/dedup/clusters").json()["clusters"][0]
    assert _member_by_sid(cluster, ALIAS)["has_beatgrid"] is True


def test_apply_still_rewrites_playlists_only(
    cue_fixture: tuple[Path, Path], dedup_db: Path,
) -> None:
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app
    from apps.webui.server.sqlite_backend import SqliteBackend

    state_path = cue_fixture[0]
    app = create_app(
        backend=SqliteBackend(state_path),
        state_db_path=str(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as client:
        clusters = client.get("/api/v1/dedup/clusters")
        assert clusters.status_code == 200
        etag = clusters.headers["etag"]
        cluster = clusters.json()["clusters"][0]
        # cue_fixture gives ALIAS hot cues CANON lacks; confirm_cue_loss opts
        # into discarding them so this test can stay focused on playlist
        # rewrite behaviour (see test_hardening_round2.py for the gate itself).
        response = client.post(
            f"/api/v1/dedup/clusters/{cluster['cluster_id']}/apply",
            headers={"If-Match": etag},
            json={
                "cluster_key": cluster["cluster_key"],
                "survivor": CANON,
                "confirm_cue_loss": True,
            },
        )
        assert response.status_code == 200


def test_bulk_cue_presence_empty_when_no_state_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rb_config, "STATE_DB", Path("/nonexistent/state.db"))
    assert bulk_cue_presence(["track-canon"]) == {}
