"""OpenDJ playlist membership apply/undo for duplicate-review-merge.

[if] merge does not rewrite every alias playlist position to the survivor [then] fail, [else stop].

Regression one-liners:
  - if merge does not rewrite every alias position to the survivor, including
    duplicate positions, then broken
  - if playlists that do not contain an alias are rewritten then broken
  - if GET playlists after apply does not show the new membership then broken
  - if undo does not restore before and GET clusters pending_apply true then broken
  - if a second apply with the same survivor is not idempotent then broken
  - if keep-all then apply is not 422 then broken
  - if a stale If-Match apply is not 409 with unchanged membership then broken
  - if apply deletes the alias audio file then broken
  - if missing state.db is not 503 then broken
  - if OpenAPI omits apply/undo If-Match as required then broken
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.dedup import schema as dedup_schema
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server import dedup_decisions
from apps.webui.server.app import create_app
from apps.webui.server.routes import dedup_review, playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.webui.test_dedup_review import _seed_cluster_db

pytestmark = [pytest.mark.requirement("CAT-05"), pytest.mark.rb_parity]

CANON = "track-canon"
ALIAS = "track-alias"
OTHER = "track-other"


def _seed_state_db(path: Path, *, canon_path: str | None = None,
                   alias_path: str | None = None) -> None:
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        writer.upsert_track(
            stable_id=CANON, stable_id_tier="inferred",
            title="Midnight Drive", artists=["Tamsin Quell"], album=None,
            isrc=None, duration_ms=210_000, file_path=canon_path,
        )
        writer.upsert_track(
            stable_id=ALIAS, stable_id_tier="inferred",
            title="Midnight Drive (128k)", artists=["Tamsin Quell"], album=None,
            isrc=None, duration_ms=210_000, file_path=alias_path,
        )
        writer.upsert_track(
            stable_id=OTHER, stable_id_tier="inferred",
            title="Unrelated", artists=["Other"], album=None,
            isrc=None, duration_ms=180_000, file_path=None,
        )
    finally:
        writer.close()
        conn.close()


@pytest.fixture
def state_db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    _seed_state_db(path)
    return path


@pytest.fixture
def dedup_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "phase7.sqlite"
    decisions_path = tmp_path / "review-decisions.json"
    monkeypatch.setattr(dedup_review.dedup_paths, "DEDUP_FALLBACK_DB", db_path)
    monkeypatch.setattr(dedup_review, "DECISIONS_FILE", decisions_path)
    monkeypatch.setattr(dedup_decisions, "DECISIONS_FILE", decisions_path)
    return db_path


@pytest.fixture
def client(state_db_path: Path, dedup_db: Path) -> Iterator[TestClient]:
    _seed_cluster_db(
        dedup_db, cluster_id=1,
        canonical_sid=CANON, canonical_path="/music/canon.flac",
        alias_sid=ALIAS, alias_path="/music/alias-128.mp3",
    )
    app = create_app(
        backend=SqliteBackend(state_db_path),
        state_db_path=str(state_db_path),
        bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None, mount_frontend=False,
    )
    with TestClient(app) as test_client:
        yield test_client
    playlist_write.close_store(app)


def _revision(client: TestClient) -> tuple[str, str]:
    response = client.get("/api/v1/dedup/clusters")
    assert response.status_code == 200, response.text
    cluster = response.json()["clusters"][0]
    return cluster["cluster_key"], response.headers["etag"]


def _create_playlist(
    client: TestClient, name: str, items: list[str],
) -> tuple[str, str]:
    created = client.post("/api/v1/playlists", json={"name": name})
    assert created.status_code == 201, created.text
    playlist_id = created.json()["playlist_id"]
    replaced = client.put(
        f"/api/v1/playlists/{playlist_id}/tracks",
        json={"stable_ids": items},
        headers={"If-Match": created.headers["ETag"]},
    )
    assert replaced.status_code == 200, replaced.text
    return playlist_id, replaced.headers["ETag"]


def _apply(
    client: TestClient, cluster_key: str, revision: str, survivor: str = CANON,
):
    return client.post(
        "/api/v1/dedup/clusters/1/apply",
        headers={"If-Match": revision},
        json={"cluster_key": cluster_key, "survivor": survivor},
    )


def _undo(
    client: TestClient, cluster_key: str, revision: str, survivor: str = CANON,
):
    return client.post(
        "/api/v1/dedup/clusters/1/undo",
        headers={"If-Match": revision},
        json={"cluster_key": cluster_key, "survivor": survivor},
    )


def _playlist_items(client: TestClient, playlist_id: str) -> list[str]:
    response = client.get(f"/api/v1/playlists/{playlist_id}")
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


def test_merge_rewrites_every_alias_position_including_duplicates(
    client: TestClient,
) -> None:
    cluster_key, revision = _revision(client)
    affected, _ = _create_playlist(
        client, "Affected", [ALIAS, OTHER, ALIAS, CANON],
    )
    untouched, _ = _create_playlist(client, "Untouched", [OTHER, CANON])
    response = _apply(client, cluster_key, revision)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "merge"
    assert body["pending_apply"] is False
    assert body["survivor"] == CANON
    rewritten = {row["playlist_id"]: row for row in body["playlists"]}
    assert affected in rewritten
    assert untouched not in rewritten
    assert rewritten[affected]["before"] == [ALIAS, OTHER, ALIAS, CANON]
    assert rewritten[affected]["after"] == [CANON, OTHER, CANON, CANON]
    assert _playlist_items(client, affected) == [CANON, OTHER, CANON, CANON]
    assert _playlist_items(client, untouched) == [OTHER, CANON]


def test_get_playlists_after_apply_shows_new_membership(client: TestClient) -> None:
    cluster_key, revision = _revision(client)
    playlist_id, _ = _create_playlist(client, "Reload", [ALIAS])
    assert _apply(client, cluster_key, revision).status_code == 200
    listed = client.get("/api/v1/playlists").json()
    assert any(row["playlist_id"] == playlist_id for row in listed)
    assert _playlist_items(client, playlist_id) == [CANON]


def test_undo_restores_before_and_pending_apply(client: TestClient) -> None:
    cluster_key, revision = _revision(client)
    playlist_id, _ = _create_playlist(client, "Undo Me", [ALIAS, OTHER])
    applied = _apply(client, cluster_key, revision)
    assert applied.status_code == 200, applied.text
    undone = _undo(client, cluster_key, applied.headers["etag"])
    assert undone.status_code == 200, undone.text
    body = undone.json()
    assert body["pending_apply"] is True
    assert playlist_id in body["playlist_ids"]
    assert _playlist_items(client, playlist_id) == [ALIAS, OTHER]
    clusters = client.get("/api/v1/dedup/clusters").json()["clusters"][0]
    assert clusters["decision"]["action"] == "merge"
    assert clusters["decision"]["pending_apply"] is True


def test_second_apply_is_idempotent(client: TestClient) -> None:
    cluster_key, revision = _revision(client)
    playlist_id, _ = _create_playlist(client, "Twice", [ALIAS, ALIAS])
    first = _apply(client, cluster_key, revision)
    assert first.status_code == 200, first.text
    second = _apply(client, cluster_key, first.headers["etag"])
    assert second.status_code == 200, second.text
    assert second.json()["pending_apply"] is False
    assert _playlist_items(client, playlist_id) == [CANON, CANON]
    undone = _undo(client, cluster_key, second.headers["etag"])
    assert undone.status_code == 200, undone.text
    assert _playlist_items(client, playlist_id) == [ALIAS, ALIAS]


def test_keep_all_then_apply_is_422(client: TestClient) -> None:
    cluster_key, revision = _revision(client)
    playlist_id, _ = _create_playlist(client, "Keep", [ALIAS])
    decided = client.post(
        "/api/v1/dedup/clusters/1/decision",
        headers={"If-Match": revision},
        json={"cluster_key": cluster_key, "survivor": CANON, "action": "keep-all"},
    )
    assert decided.status_code == 200, decided.text
    applied = _apply(client, cluster_key, decided.headers["etag"])
    assert applied.status_code == 422
    assert applied.json()["detail"]["error"] == "decision_is_not_merge"
    assert _playlist_items(client, playlist_id) == [ALIAS]


def test_stale_if_match_leaves_membership_unchanged(client: TestClient) -> None:
    cluster_key, stale = _revision(client)
    playlist_id, _ = _create_playlist(client, "Stale", [ALIAS])
    first = client.post(
        "/api/v1/dedup/clusters/1/decision",
        headers={"If-Match": stale},
        json={"cluster_key": cluster_key, "survivor": CANON, "action": "skip"},
    )
    assert first.status_code == 200
    applied = _apply(client, cluster_key, stale)
    assert applied.status_code == 409
    assert _playlist_items(client, playlist_id) == [ALIAS]


def test_apply_does_not_delete_alias_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    canon_file = tmp_path / "canon.flac"
    alias_file = tmp_path / "alias.mp3"
    canon_file.write_bytes(b"canon-audio")
    alias_file.write_bytes(b"alias-audio")
    state_path = tmp_path / "state.db"
    _seed_state_db(state_path, canon_path=str(canon_file), alias_path=str(alias_file))
    db_path = tmp_path / "phase7.sqlite"
    decisions_path = tmp_path / "review-decisions.json"
    monkeypatch.setattr(dedup_review.dedup_paths, "DEDUP_FALLBACK_DB", db_path)
    monkeypatch.setattr(dedup_review, "DECISIONS_FILE", decisions_path)
    monkeypatch.setattr(dedup_decisions, "DECISIONS_FILE", decisions_path)
    _seed_cluster_db(
        db_path, cluster_id=1,
        canonical_sid=CANON, canonical_path=str(canon_file),
        alias_sid=ALIAS, alias_path=str(alias_file),
    )
    app = create_app(
        backend=SqliteBackend(state_path),
        state_db_path=str(state_path),
        bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None, mount_frontend=False,
    )
    with TestClient(app) as test_client:
        cluster_key, revision = _revision(test_client)
        _create_playlist(test_client, "Files", [ALIAS])
        applied = _apply(test_client, cluster_key, revision)
        assert applied.status_code == 200, applied.text
        assert canon_file.is_file()
        assert alias_file.is_file()
        assert canon_file.read_bytes() == b"canon-audio"
        assert alias_file.read_bytes() == b"alias-audio"
    playlist_write.close_store(app)


def test_missing_state_db_is_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.webui.server.backend import InMemoryBackend

    db_path = tmp_path / "phase7.sqlite"
    decisions_path = tmp_path / "review-decisions.json"
    monkeypatch.setattr(dedup_review.dedup_paths, "DEDUP_FALLBACK_DB", db_path)
    monkeypatch.setattr(dedup_review, "DECISIONS_FILE", decisions_path)
    monkeypatch.setattr(dedup_decisions, "DECISIONS_FILE", decisions_path)
    dedup_schema.ensure_schema(db_path).close()
    missing = tmp_path / "missing-state.db"
    app = create_app(
        backend=InMemoryBackend(),
        state_db_path=str(missing),
        bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None, mount_frontend=False,
    )
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/dedup/clusters/1/apply",
            headers={"If-Match": '"empty"'},
            json={"cluster_key": "sha256:missing", "survivor": CANON},
        )
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "state_db_missing"


def test_openapi_documents_apply_undo_if_match(client: TestClient) -> None:
    paths = client.app.openapi()["paths"]
    for suffix in ("apply", "undo"):
        operation = paths[f"/api/v1/dedup/clusters/{{cluster_id}}/{suffix}"]["post"]
        if_match = next(
            parameter
            for parameter in operation["parameters"]
            if parameter["in"] == "header" and parameter["name"] == "If-Match"
        )
        assert if_match["required"] is True
        assert {"200", "409", "428"} <= set(operation["responses"])
