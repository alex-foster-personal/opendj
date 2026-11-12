"""Stem A/B/C labelling HTTP surface (#11193).

[if] label-tasks routes are exercised [then] clip GETs use real bundles and batch consensus fails open, [else stop].
"""

from __future__ import annotations

import json
import sqlite3
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import schema as state_schema
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.deps import get_write_state
from apps.webui.server.routes import label_tasks as label_tasks_routes
from apps.webui.server.routes.stems import router as stems_router

pytestmark = pytest.mark.requirement("INTEL-13572")

_TASK_ID = "task-stem-abc-1"
_NOW = "2026-11-12T12:00:00+00:00"


def _write_wav(path: Path, *, frames: int = 8) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(44_100)
        output.writeframes(b"\x00\x00" * frames)


def _bundle(roots: Path, stable_id: str) -> None:
    bundle = roots / stable_id
    bundle.mkdir(parents=True, exist_ok=True)
    for part in ("vocals", "drums", "bass", "other"):
        _write_wav(bundle / f"{part}.wav")
    manifest = {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": "C:/Music/Example.wav", "sha256": "a" * 64},
        "files": {
            "vocals": "vocals.wav",
            "drums": "drums.wav",
            "bass": "bass.wav",
            "other": "other.wav",
        },
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _payload() -> dict:
    return {
        "kind": "stems_ab_rank",
        "clips": [
            {"id": "A", "stable_id": "clip-a", "part": "vocals"},
            {"id": "B", "stable_id": "clip-b", "part": "vocals"},
            {"id": "C", "stable_id": "clip-c", "part": "vocals"},
        ],
    }


def _seed_task(conn: sqlite3.Connection, *, batch_size: int = 3, created_at: str = _NOW) -> None:
    conn.execute(
        """
        INSERT INTO label_task (
            id, experiment_id, hypothesis_ids, modality, payload,
            reward_karma, max_wait_s, batch_size, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _TASK_ID,
            "exp-stem-quality",
            json.dumps(["hyp-stem-farm"]),
            "stems_ab_rank",
            json.dumps(_payload()),
            5,
            120,
            batch_size,
            created_at,
        ),
    )
    conn.commit()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    stems_dir = tmp_path / "stems"
    state_db = tmp_path / "state.db"
    for sid in ("clip-a", "clip-b", "clip-c"):
        _bundle(stems_dir, sid)
    conn = sqlite3.connect(state_db)
    state_schema._ensure_meta(conn)
    state_schema.apply_migrations(conn)
    _seed_task(conn)
    conn.close()

    app = FastAPI()
    app.state.stems_dir = stems_dir
    app.state.state_db_path = str(state_db)

    app.dependency_overrides[get_write_state] = lambda: InMemoryBackend()
    app.include_router(stems_router, prefix="/api/v1")
    app.include_router(label_tasks_routes.router, prefix="/api/v1")
    return TestClient(app)


def test_get_stem_abc_task_exposes_real_clip_urls(client: TestClient) -> None:
    """[if] GET a stem A/B/C task [then] clip URLs point at validated bundles, [else stop]."""
    body = client.get(f"/api/v1/label-tasks/{_TASK_ID}").json()
    assert len(body["clips"]) == 3
    for clip in body["clips"]:
        stem = client.get(clip["url"])
        assert stem.status_code == 200
        assert stem.content.startswith(b"RIFF")


def test_post_ranking_strength_and_cost_choice(client: TestClient) -> None:
    """[if] POST ranking plus strength plus cost_choice [then] store response, [else stop]."""
    resp = client.post(
        f"/api/v1/label-tasks/{_TASK_ID}/responses",
        json={
            "anon_session": "anon-1",
            "ranking": ["B", "A", "C"],
            "strength": 0.7,
            "cost_choice": "worth_extra_cost",
            "ms_to_complete": 31000,
        },
    )
    assert resp.status_code == 201
    conn = sqlite3.connect(client.app.state.state_db_path)
    try:
        row = conn.execute(
            "SELECT ranking, strength, cost_choice FROM label_response WHERE task_id = ?",
            (_TASK_ID,),
        ).fetchone()
        assert row == (json.dumps(["B", "A", "C"]), 0.7, "worth_extra_cost")
        ledger = conn.execute("SELECT karma_delta FROM contribution_ledger").fetchone()
        assert ledger == (5,)
    finally:
        conn.close()


def test_incomplete_batch_consensus_is_insufficient_labels(client: TestClient) -> None:
    """[if] a caller asks for consensus with an incomplete batch [then] insufficient_labels, [else stop]."""
    client.post(
        f"/api/v1/label-tasks/{_TASK_ID}/responses",
        json={
            "anon_session": "anon-a",
            "ranking": ["A", "B", "C"],
            "strength": 0.5,
            "cost_choice": "cheap",
            "ms_to_complete": 1000,
        },
    )
    out = client.get(f"/api/v1/label-tasks/{_TASK_ID}/consensus").json()
    assert out["status"] == "insufficient_labels"
    assert out.get("consensus") is None


def test_complete_batch_returns_consensus_without_inventing(client: TestClient) -> None:
    """[if] batch_size responses exist [then] consensus aggregates real answers, [else stop]."""
    for idx, ranking in enumerate((["A", "B", "C"], ["B", "A", "C"], ["B", "A", "C"])):
        client.post(
            f"/api/v1/label-tasks/{_TASK_ID}/responses",
            json={
                "anon_session": f"anon-{idx}",
                "ranking": ranking,
                "strength": 0.5 + idx * 0.1,
                "cost_choice": "worth_extra_cost" if idx < 2 else "cheap",
                "ms_to_complete": 2000,
            },
        )
    out = client.get(f"/api/v1/label-tasks/{_TASK_ID}/consensus").json()
    assert out["status"] == "ready"
    assert out["consensus"]["ranking"][0] == "B"
    assert out["consensus"]["cost_choice"] == "worth_extra_cost"


def test_max_wait_elapsed_with_short_batch_stays_insufficient(tmp_path: Path) -> None:
    """[if] fewer than batch_size responses arrive before max_wait_s [then] fail open, [else stop]."""
    stems_dir = tmp_path / "stems"
    state_db = tmp_path / "state.db"
    for sid in ("clip-a", "clip-b", "clip-c"):
        _bundle(stems_dir, sid)
    conn = sqlite3.connect(state_db)
    state_schema._ensure_meta(conn)
    state_schema.apply_migrations(conn)
    old = (datetime.now(tz=UTC) - timedelta(seconds=300)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    _seed_task(conn, batch_size=4, created_at=old)
    conn.close()

    app = FastAPI()
    app.state.stems_dir = stems_dir
    app.state.state_db_path = str(state_db)
    app.dependency_overrides[get_write_state] = lambda: InMemoryBackend()
    app.include_router(label_tasks_routes.router, prefix="/api/v1")
    cli = TestClient(app)
    cli.post(
        f"/api/v1/label-tasks/{_TASK_ID}/responses",
        json={
            "anon_session": "lonely",
            "ranking": ["C", "B", "A"],
            "strength": 0.3,
            "cost_choice": "skip",
            "ms_to_complete": 900,
        },
    )
    out = cli.get(f"/api/v1/label-tasks/{_TASK_ID}/consensus").json()
    assert out["status"] == "insufficient_labels"
    assert "max_wait_s" in (out.get("reason") or "")
