"""Deck audio route uses believed-state resolution (CLOUDSYNC-10).

[if] deck audio is requested for a track with a believed-state location [then] the route resolves through resolve_playback_source, [else stop].
[if] deck audio resolution ignores believed-state rules [then] routes serve wrong or missing audio, [else stop].
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.cloud import job as cloud_job
from apps.engine_core.jobs.store import JobStore
from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend

pytestmark = pytest.mark.requirement("CLOUDSYNC-10")

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_AUDIO = (
    REPO_ROOT / "tests" / "fixtures" / "conformance" / "03-8-hot-cues" / "audio" / "cues.mp3"
)
PLAYABLE_SID = "e" * 40
UNAVAILABLE_SID = "f" * 40
REMOTE_SID = "b" * 40
NO_HASH_SID = "c" * 40


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _seed_believed_state(state_path: Path) -> None:
    playable_digest = _sha(REAL_AUDIO.read_bytes())
    unavailable_digest = _sha(b"missing")
    gone_path = state_path.parent / "gone.mp3"
    conn = state_db.open_rw(state_path)
    try:
        machine_id = sync_stamp.ensure_local_machine(conn)
        conn.execute(
            "INSERT INTO sync_policies(machine_id, asset_kind, mode, cache_budget_mb, "
            "updated_at) VALUES (?, 'audio', 'pinned', 100, '2026-01-01')",
            (machine_id,),
        )
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, file_path, "
            "content_hash, created_at, updated_at) "
            "VALUES (?, 'inferred', 210000, ?, ?, '2026-01-01', '2026-01-01')",
            (PLAYABLE_SID, str(REAL_AUDIO), playable_digest),
        )
        conn.execute(
            "INSERT INTO track_locations(stable_id, machine_id, kind, role, file_path, "
            "available, content_hash, created_at, updated_at) "
            "VALUES (?, ?, 'local', 'primary', ?, 1, ?, '2026-01-01', '2026-01-01')",
            (PLAYABLE_SID, machine_id, str(REAL_AUDIO), playable_digest),
        )
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, file_path, "
            "content_hash, created_at, updated_at) "
            "VALUES (?, 'inferred', 210000, ?, ?, '2026-01-01', '2026-01-01')",
            (UNAVAILABLE_SID, str(gone_path), unavailable_digest),
        )
        conn.execute(
            "INSERT INTO track_locations(stable_id, machine_id, kind, role, file_path, "
            "available, content_hash, created_at, updated_at) "
            "VALUES (?, ?, 'local', 'primary', ?, 0, ?, '2026-01-01', '2026-01-01')",
            (UNAVAILABLE_SID, machine_id, str(gone_path), unavailable_digest),
        )
        remote_digest = _sha(b"remote-only-body")
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, file_path, "
            "content_hash, created_at, updated_at) "
            "VALUES (?, 'inferred', 210000, NULL, ?, '2026-01-01', '2026-01-01')",
            (REMOTE_SID, remote_digest),
        )
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, file_path, "
            "content_hash, created_at, updated_at) "
            "VALUES (?, 'inferred', 210000, NULL, NULL, '2026-01-01', '2026-01-01')",
            (NO_HASH_SID,),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_stream_policy(state_path: Path) -> None:
    conn = state_db.open_rw(state_path)
    try:
        machine_id = sync_stamp.ensure_local_machine(conn)
        conn.execute(
            "INSERT OR IGNORE INTO sync_policies(machine_id, asset_kind, mode, "
            "cache_budget_mb, updated_at) VALUES (?, 'audio', 'stream', 100, "
            "'2026-01-01')",
            (machine_id,),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    assert REAL_AUDIO.is_file()
    state_path = tmp_path / "state.db"
    _seed_believed_state(state_path)
    _seed_stream_policy(state_path)
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent-master.db")
    monkeypatch.setattr(rb_config, "DATA_DIR", tmp_path)
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
    )
    with TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1") as tc:
        yield tc


@pytest.fixture
def hydrating_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, JobStore]]:
    for name, value in {
        "R2_ACCOUNT_ID": "acct",
        "R2_ACCESS_KEY_ID": "key",
        "R2_SECRET_ACCESS_KEY": "secret",
    }.items():
        monkeypatch.setenv(name, value)
    state_path = tmp_path / "state.db"
    _seed_believed_state(state_path)
    _seed_stream_policy(state_path)
    jobs_store = JobStore(
        tmp_path / "jobs.db", boot_id="boot-hydrate-audio", owner_pid=os.getpid()
    )
    jobs_store.recover()
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent-master.db")
    monkeypatch.setattr(rb_config, "DATA_DIR", tmp_path)
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
    )
    app.state.jobs_store = jobs_store
    with TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1") as tc:
        yield tc, jobs_store
    jobs_store.close()


def test_playable_track_serves_audio(client: TestClient) -> None:
    response = client.get(
        f"/api/v1/tracks/{PLAYABLE_SID}/audio",
        headers={"Range": "bytes=0-0"},
    )
    assert response.status_code == 206
    assert response.headers["content-type"].startswith("audio/")


def test_unavailable_local_row_refuses_play(client: TestClient) -> None:
    response = client.get(f"/api/v1/tracks/{UNAVAILABLE_SID}/audio")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "CLOUD_ASSET_UNAVAILABLE"
    assert "available=0" in detail["message"]


def test_remote_only_track_returns_cloud_hydrating_and_enqueues_job(
    hydrating_client: tuple[TestClient, JobStore],
) -> None:
    client, jobs_store = hydrating_client
    before = [
        job
        for job in jobs_store.list()
        if job["kind"] == cloud_job.JOB_KIND
        and job["payload"].get("stable_id") == REMOTE_SID
    ]
    response = client.get(f"/api/v1/tracks/{REMOTE_SID}/audio")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "CLOUD_HYDRATING"
    after = [
        job
        for job in jobs_store.list()
        if job["kind"] == cloud_job.JOB_KIND
        and job["payload"].get("stable_id") == REMOTE_SID
    ]
    assert len(after) == len(before) + 1
    assert after[0]["status"] == "queued"


def test_missing_content_hash_refuses_audio_route(client: TestClient) -> None:
    response = client.get(f"/api/v1/tracks/{NO_HASH_SID}/audio")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "CLOUD_ASSET_UNAVAILABLE"
    assert "content_hash" in detail["message"]


@pytest.fixture
def unconfigured_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """A machine nobody configured CloudSync on: no ``sync_policies`` rows."""
    state_path = tmp_path / "state.db"
    _seed_believed_state(state_path)
    conn = state_db.open_rw(state_path)
    try:
        conn.execute("DELETE FROM sync_policies")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent-master.db")
    monkeypatch.setattr(rb_config, "DATA_DIR", tmp_path)
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
    )
    with TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1") as tc:
        yield tc


@pytest.mark.requirement("CLOUDSYNC-33")
def test_unconfigured_machine_says_a_missing_file_is_not_on_this_computer(
    unconfigured_client: TestClient,
) -> None:
    """[if] a machine with no CloudSync policy loads a track it has no copy of
    [then] the route answers 404 AUDIO_NOT_ON_THIS_MACHINE with a plain
    sentence, not CLOUD_POLICY_UNCONFIGURED, [else stop]."""
    response = unconfigured_client.get(f"/api/v1/tracks/{REMOTE_SID}/audio")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "AUDIO_NOT_ON_THIS_MACHINE"
    assert detail["message"] == "This file isn't on this computer."
    # Control: the same machine still plays its own copy.
    played = unconfigured_client.get(
        f"/api/v1/tracks/{PLAYABLE_SID}/audio", headers={"Range": "bytes=0-0"}
    )
    assert played.status_code == 206


@pytest.mark.requirement("CLOUDSYNC-33")
def test_unconfigured_machine_plays_rekordboxs_copy_when_its_own_path_is_gone(
    unconfigured_client: TestClient, tmp_path: Path
) -> None:
    """[if] a local-only machine's own location for a track is gone but
    rekordbox's FolderPath for it is on disk [then] the deck plays rekordbox's
    file, as the listing already counts it available, [else stop]."""
    import sqlite3

    master = tmp_path / "absent-master.db"
    conn = sqlite3.connect(master)
    try:
        conn.executescript(
            "CREATE TABLE djmdContent (ID TEXT, FolderPath TEXT, ImagePath TEXT, "
            "AnalysisDataPath TEXT, Length INTEGER, Commnt TEXT, GenreID TEXT, "
            "rb_local_deleted INTEGER DEFAULT 0);"
            "CREATE TABLE djmdGenre (ID TEXT, Name TEXT, rb_local_deleted INTEGER DEFAULT 0);"
        )
        conn.execute(
            "INSERT INTO djmdContent (ID, FolderPath) VALUES ('208807409', ?)",
            (str(REAL_AUDIO),),
        )
        conn.commit()
    finally:
        conn.close()
    state = state_db.open_rw(rb_config.STATE_DB)
    try:
        state.execute(
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) "
            "VALUES (?, 'rekordbox', '208807409')",
            (UNAVAILABLE_SID,),
        )
        state.commit()
    finally:
        state.close()

    played = unconfigured_client.get(
        f"/api/v1/tracks/{UNAVAILABLE_SID}/audio", headers={"Range": "bytes=0-0"}
    )
    assert played.status_code == 206
    # Control: a track with neither its own copy nor a rekordbox one still
    # says, plainly, that it is not here.
    missing = unconfigured_client.get(f"/api/v1/tracks/{REMOTE_SID}/audio")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "AUDIO_NOT_ON_THIS_MACHINE"
