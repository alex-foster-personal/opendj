"""Hardening round 2: dedup apply must not silently discard cue/beatgrid data.

Follows the mytag.py confirm_merge precedent (block a destructive merge by
default, require an explicit opt-in flag to proceed).

Regression one-liners:
  - if apply discards cue/hot-cue/beatgrid data the survivor lacks without
    confirm_cue_loss then broken
  - if a blocked apply (no confirm_cue_loss) still rewrites playlists or
    records a decision then broken
  - if confirm_cue_loss: true does not let the merge proceed as before then
    broken

[if] dedup apply would discard survivor-lacking cue data [then] block without confirm_cue_loss, [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.webui.server import dedup_decisions
from apps.webui.server.app import create_app
from apps.webui.server.routes import dedup_review, playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.webui.test_dedup_cue_presence import (
    ALIAS_VENDOR,
    CANON_VENDOR,
    _insert_cue,
    _seed_state_db,
)
from tests.webui.test_dedup_review import _seed_cluster_db

pytestmark = [pytest.mark.requirement("CAT-05"), pytest.mark.rb_parity]

CANON = "track-canon"
ALIAS = "track-alias"


def _make_master_plain_db(path: Path) -> None:
    """Fuller djmdContent/djmdGenre schema than the cue-presence-only fixture.

    This test round-trips real playlist GET responses (rb metadata
    enrichment joins djmdContent to djmdGenre), unlike the pure cue-presence
    tests, so the fake master db needs those extra columns/tables populated
    with NULLs rather than just the cue-only shape.
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


@pytest.fixture
def dedup_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "phase7.sqlite"
    decisions_path = tmp_path / "review-decisions.json"
    monkeypatch.setattr(dedup_review.dedup_paths, "DEDUP_FALLBACK_DB", db_path)
    monkeypatch.setattr(dedup_review, "DECISIONS_FILE", decisions_path)
    monkeypatch.setattr(dedup_decisions, "DECISIONS_FILE", decisions_path)
    return db_path


@pytest.fixture
def client(
    tmp_path: Path, dedup_db: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Cluster where the alias holds hot cues the survivor lacks."""
    state_path = tmp_path / "state.db"
    master_path = tmp_path / "master.plain.db"
    _make_master_plain_db(master_path)
    _seed_state_db(state_path)
    _insert_cue(master_path, cue_id="cue-1", content_id=ALIAS_VENDOR, kind=1, in_msec=30_000)
    _insert_cue(master_path, cue_id="cue-2", content_id=ALIAS_VENDOR, kind=1, in_msec=90_000)
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master_path)
    _seed_cluster_db(
        dedup_db, cluster_id=1,
        canonical_sid=CANON, canonical_path="/music/canon.flac",
        alias_sid=ALIAS, alias_path="/music/alias-128.mp3",
    )
    app = create_app(
        backend=SqliteBackend(state_path),
        state_db_path=str(state_path),
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


def _create_playlist(client: TestClient, name: str, items: list[str]) -> str:
    created = client.post("/api/v1/playlists", json={"name": name})
    assert created.status_code == 201, created.text
    playlist_id = created.json()["playlist_id"]
    replaced = client.put(
        f"/api/v1/playlists/{playlist_id}/tracks",
        json={"stable_ids": items},
        headers={"If-Match": created.headers["ETag"]},
    )
    assert replaced.status_code == 200, replaced.text
    return playlist_id


def _playlist_items(client: TestClient, playlist_id: str) -> list[str]:
    response = client.get(f"/api/v1/playlists/{playlist_id}")
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


def _apply(
    client: TestClient, cluster_key: str, revision: str, *, confirm_cue_loss: bool | None = None,
) -> Any:
    body: dict[str, Any] = {"cluster_key": cluster_key, "survivor": CANON}
    if confirm_cue_loss is not None:
        body["confirm_cue_loss"] = confirm_cue_loss
    return client.post(
        "/api/v1/dedup/clusters/1/apply",
        headers={"If-Match": revision},
        json=body,
    )


# REQ: LIBM-76
def test_apply_without_confirmation_blocks_cue_loss(client: TestClient) -> None:
    cluster_key, revision = _revision(client)
    playlist_id = _create_playlist(client, "Cued", [ALIAS])

    response = _apply(client, cluster_key, revision)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "DEDUP_CUE_LOSS_REQUIRES_CONFIRMATION"
    lost_by_sid = {entry["stable_id"]: entry["lost_fields"] for entry in detail["cue_loss"]}
    assert ALIAS in lost_by_sid
    assert "cue_count" in lost_by_sid[ALIAS]
    assert "hot_cue_count" in lost_by_sid[ALIAS]

    # No write happened: playlist membership and decision store are untouched.
    assert _playlist_items(client, playlist_id) == [ALIAS]
    clusters_after = client.get("/api/v1/dedup/clusters").json()["clusters"][0]
    assert clusters_after["decision"] is None


def test_apply_with_confirm_cue_loss_proceeds(client: TestClient) -> None:
    cluster_key, revision = _revision(client)
    playlist_id = _create_playlist(client, "Cued", [ALIAS])

    response = _apply(client, cluster_key, revision, confirm_cue_loss=True)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["survivor"] == CANON
    assert body["pending_apply"] is False
    assert _playlist_items(client, playlist_id) == [CANON]
