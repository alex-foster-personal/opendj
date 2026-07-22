"""Tests for the dedup review routes (duplicate-review-merge).

Regression one-liners:
  - if GET /dedup/clusters doesn't skip cleanly (200 + note) without a
    fingerprint db then broken
  - if GET /dedup/clusters doesn't hydrate members from the backend then broken
  - if the canonical member isn't flagged is_canonical=True then broken
  - if POST decision doesn't round-trip on the next GET then broken
  - if POST decision accepts a survivor outside the cluster then broken
  - if POST decision on an unknown cluster_id doesn't 404 then broken
  - if GET /dedup/clusters doesn't 200 (not 500) when the db exists but is empty then broken
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.dedup import schema as dedup_schema
from apps.webui.server.backend import InMemoryBackend, Track
from apps.webui.server.routes import dedup_review

pytestmark = pytest.mark.requirement("CAT-05")


def _seed_cluster_db(
    db_path: Path, *, cluster_id: int, canonical_sid: str, canonical_path: str,
    alias_sid: str, alias_path: str, similarity: float = 0.97,
    rationale: str = "bitrate=320",
) -> None:
    conn = dedup_schema.ensure_schema(db_path)
    try:
        conn.execute(
            "INSERT INTO duplicate_clusters "
            "(cluster_id, canonical_stable_id, canonical_path, rationale) "
            "VALUES (?, ?, ?, ?)",
            (cluster_id, canonical_sid, canonical_path, rationale),
        )
        conn.execute(
            "INSERT INTO track_aliases "
            "(alias_stable_id, alias_path, cluster_id, canonical_stable_id, similarity) "
            "VALUES (?, ?, ?, ?, ?)",
            (alias_sid, alias_path, cluster_id, canonical_sid, similarity),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def seeded_backend() -> InMemoryBackend:
    backend = InMemoryBackend()
    backend.seed_track(Track(
        stable_id="track-canon", title="Midnight Drive", artist="the maintainer",
        bpm=124.0, key="8A", duration_ms=210_000, rating=4,
    ))
    backend.seed_track(Track(
        stable_id="track-alias", title="Midnight Drive (128k)", artist="the maintainer",
        bpm=124.0, key="8A", duration_ms=210_000, rating=None,
    ))
    return backend


@pytest.fixture
def dedup_db(tmp_path: Path, monkeypatch) -> Path:
    db_path = tmp_path / "phase7.sqlite"
    decisions_path = tmp_path / "review-decisions.json"
    monkeypatch.setattr(dedup_review.dedup_paths, "DEDUP_FALLBACK_DB", db_path)
    monkeypatch.setattr(dedup_review, "DECISIONS_FILE", decisions_path)
    return db_path


@pytest.fixture
def app_client(seeded_backend: InMemoryBackend, dedup_db: Path):
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app

    app = create_app(
        backend=seeded_backend, bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None,
    )
    with TestClient(app) as c:
        yield c


def test_clusters_skip_cleanly_without_db(app_client) -> None:
    r = app_client.get("/api/v1/dedup/clusters")
    assert r.status_code == 200
    body = r.json()
    assert body["clusters"] == []
    assert body["note"] is not None
    assert "apps.dedup.scan" in body["note"]


def test_clusters_empty_db_is_200(app_client, dedup_db: Path) -> None:
    dedup_schema.ensure_schema(dedup_db).close()
    r = app_client.get("/api/v1/dedup/clusters")
    assert r.status_code == 200
    assert r.json()["clusters"] == []
    assert r.json()["note"] is None


def test_clusters_hydrate_members(app_client, dedup_db: Path) -> None:
    _seed_cluster_db(
        dedup_db, cluster_id=1,
        canonical_sid="track-canon", canonical_path="/music/canon.flac",
        alias_sid="track-alias", alias_path="/music/alias-128.mp3",
    )
    r = app_client.get("/api/v1/dedup/clusters")
    assert r.status_code == 200
    clusters = r.json()["clusters"]
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["cluster_id"] == 1
    assert cluster["survivor_stable_id"] == "track-canon"
    assert cluster["rationale"] == "bitrate=320"
    assert cluster["flagged_manual_review"] is False
    assert cluster["decision"] is None

    by_id = {m["stable_id"]: m for m in cluster["members"]}
    assert by_id["track-canon"]["is_canonical"] is True
    assert by_id["track-canon"]["title"] == "Midnight Drive"
    assert by_id["track-canon"]["similarity"] is None
    assert by_id["track-alias"]["is_canonical"] is False
    assert by_id["track-alias"]["similarity"] == pytest.approx(0.97)
    assert by_id["track-alias"]["artist"] == "the maintainer"
    # Neither fixture path exists on disk.
    assert by_id["track-canon"]["file_exists"] is False
    assert by_id["track-alias"]["file_exists"] is False


def test_decision_round_trip(app_client, dedup_db: Path) -> None:
    _seed_cluster_db(
        dedup_db, cluster_id=7,
        canonical_sid="track-canon", canonical_path="/music/canon.flac",
        alias_sid="track-alias", alias_path="/music/alias-128.mp3",
    )
    r = app_client.post(
        "/api/v1/dedup/clusters/7/decision",
        json={"survivor": "track-alias", "action": "merge"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["cluster_id"] == 7
    assert body["survivor"] == "track-alias"
    assert body["action"] == "merge"
    assert body["pending_apply"] is True
    assert body["decided_at"]

    r2 = app_client.get("/api/v1/dedup/clusters")
    decision = r2.json()["clusters"][0]["decision"]
    assert decision["survivor"] == "track-alias"
    assert decision["action"] == "merge"


def test_decision_rejects_non_member_survivor(app_client, dedup_db: Path) -> None:
    _seed_cluster_db(
        dedup_db, cluster_id=1,
        canonical_sid="track-canon", canonical_path="/music/canon.flac",
        alias_sid="track-alias", alias_path="/music/alias-128.mp3",
    )
    r = app_client.post(
        "/api/v1/dedup/clusters/1/decision",
        json={"survivor": "not-in-cluster", "action": "merge"},
    )
    assert r.status_code == 422


def test_decision_unknown_cluster_404(app_client, dedup_db: Path) -> None:
    dedup_schema.ensure_schema(dedup_db).close()
    r = app_client.post(
        "/api/v1/dedup/clusters/999/decision",
        json={"survivor": "track-canon", "action": "skip"},
    )
    assert r.status_code == 404


def test_decision_no_db_404(app_client) -> None:
    r = app_client.post(
        "/api/v1/dedup/clusters/1/decision",
        json={"survivor": "track-canon", "action": "skip"},
    )
    assert r.status_code == 404


def test_decision_rejects_bad_action(app_client, dedup_db: Path) -> None:
    _seed_cluster_db(
        dedup_db, cluster_id=1,
        canonical_sid="track-canon", canonical_path="/music/canon.flac",
        alias_sid="track-alias", alias_path="/music/alias-128.mp3",
    )
    r = app_client.post(
        "/api/v1/dedup/clusters/1/decision",
        json={"survivor": "track-canon", "action": "delete-everything"},
    )
    assert r.status_code == 422
