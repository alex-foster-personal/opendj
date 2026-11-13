"""GET /api/sets/{session_id}/controls returns one session's recorded v2 control timeline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sets.control_events import ControlEventV2
from apps.sets.manifest import Manifest, write_manifest
from tests.sets.conftest import mount_sets_router

REQ = "SETREC-14729"


@pytest.fixture
def controls_client(sets_root: Path, sets_state_db: Path):
    """Real sets router bound to a tmp sets root."""
    app = FastAPI()
    app.state.sets_root = sets_root
    app.state.state_db_path = str(sets_state_db)
    mount_sets_router(app)
    with TestClient(app) as client:
        yield client, sets_root


def _event(session_id: str, seq: int) -> ControlEventV2:
    return ControlEventV2(
        session_id=session_id,
        seq=seq,
        sample=seq * 480,
        sample_rate=48000,
        wall_clock=f"2026-11-13T07:00:0{seq}Z",
        control="mixer.crossfader",
        value=seq / 10,
        kind="set",
        source="user",
    )


def _seed_session(sets_root: Path, session_id: str, seqs: list[int]) -> Path:
    session_dir = sets_root / session_id
    session_dir.mkdir(parents=True)
    write_manifest(
        session_dir,
        Manifest(session_id=session_id, started_at="2026-11-13T07:00:00+00:00", ended_at=None, capture_device="test"),
    )
    lines = [json.dumps(_event(session_id, seq).to_json()) for seq in seqs]
    (session_dir / "controls.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return session_dir


@pytest.mark.requirement(REQ)
def test_controls_returns_persisted_events_in_ascending_seq(controls_client) -> None:
    """[if] a session has persisted control events [then] GET returns them in ascending seq order, [else stop]."""
    client, sets_root = controls_client
    _seed_session(sets_root, "s1", [2, 0, 1])

    response = client.get("/api/sets/s1/controls")

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "s1"
    assert [row["seq"] for row in body["events"]] == [0, 1, 2]
    assert body["events"][0] == _event("s1", 0).to_json()


@pytest.mark.requirement(REQ)
def test_controls_missing_session_answers_sets_not_found_shape(controls_client) -> None:
    """[if] a requested session does not exist [then] GET returns the sets 404 not-found shape, [else stop]."""
    client, _ = controls_client

    response = client.get("/api/sets/nope/controls")
    reference = client.get("/api/sets/nope")

    assert response.status_code == 404
    assert response.json() == {"detail": "session not found"} == reference.json()


@pytest.mark.requirement(REQ)
@pytest.mark.parametrize(("encoded_session_id", "expected_status"), [("..%5Coutside", 400), ("..%2Foutside", 404)])
def test_controls_encoded_traversal_never_reads_outside_sets_root(
    controls_client, encoded_session_id: str, expected_status: int
) -> None:
    """[if] a session id carries encoded traversal [then] GET refuses without reading outside the sets root, [else stop]."""
    client, sets_root = controls_client
    _seed_session(sets_root.parent, "outside", [0])

    response = client.get(f"/api/sets/{encoded_session_id}/controls")

    assert response.status_code == expected_status
    assert "events" not in response.json()
