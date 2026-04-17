"""Tests for :mod:`apps.sets.api` (Plan 12-03 FastAPI router)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sets import api as api_mod
from apps.sets import paths as sets_paths_mod
from apps.sets.manifest import AudioSegment, Manifest, write_manifest


@pytest.fixture
def api_test_client(sets_root: Path, monkeypatch):
    """A FastAPI TestClient pointing at a temp SETS_DIR."""
    monkeypatch.setattr(sets_paths_mod, "SETS_DIR", sets_root)
    app = FastAPI()
    app.include_router(api_mod.router)
    with TestClient(app) as client:
        yield client, sets_root


def _seed_api_session(sets_root: Path, session_id: str = "s1", *, private: bool = True) -> Path:
    sess = sets_root / session_id
    sess.mkdir(parents=True, exist_ok=True)
    share = "private" if private else "shared_local"
    mp3 = sess / "audio_2026-04-17T21-30-00.mp3"
    mp3.write_bytes(b"\xff\xfb" + b"\x00" * 1024)
    write_manifest(
        sess,
        Manifest(
            session_id=session_id,
            started_at="2026-04-17T21:30:00+00:00",
            ended_at="2026-04-17T23:00:00+00:00",
            capture_device="BlackHole 2ch",
            share_state=share,
            event_count=2,
            deck_sources=["djay_monitor"],
            mp3_segments=[AudioSegment(name=mp3.name, start_t_s=0.0, duration_s=300.0, size_bytes=mp3.stat().st_size)],
        ),
    )
    # timeline.jsonl
    timeline = [
        {"session_id": session_id, "timestamp_s": 0.0,
         "wall_clock": "2026-04-17T21:30:00+00:00",
         "action": "session_start", "source": "recorder", "deck": None,
         "track_stable_id": None, "value": {}},
        {"session_id": session_id, "timestamp_s": 5.0,
         "wall_clock": "2026-04-17T21:30:05+00:00",
         "action": "track_loaded", "source": "djay_monitor", "deck": "A",
         "track_stable_id": "uuid-1", "value": {"title": "Track 1"}},
    ]
    (sess / "timeline.jsonl").write_text(
        "\n".join(json.dumps(t) for t in timeline) + "\n"
    )
    # transitions.jsonl
    transitions = [
        {"idx": 0, "t_start_s": 0.0, "t_change_s": 10.0, "t_end_s": 12.0,
         "from_deck": "A", "to_deck": "B", "from_track": "t1", "to_track": "t2",
         "predicted_class": "cut", "confidence": 0.9, "model_version": "rules-v0",
         "features": {"overlap_s": 0.2, "fade_s": 0.1,
                      "incoming_preload_s": 1.0, "outgoing_trail_s": 0.1,
                      "time_since_prev_transition_s": 0.0,
                      "is_same_deck_reload": 0.0}},
    ]
    (sess / "transitions.jsonl").write_text(
        "\n".join(json.dumps(t) for t in transitions) + "\n"
    )
    return sess


@pytest.mark.requirement("SET-03")
def test_api_list_sessions_returns_array(api_test_client):
    client, sets_root = api_test_client
    _seed_api_session(sets_root)
    resp = client.get("/api/sets")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert data[0]["session_id"] == "s1"


@pytest.mark.requirement("SET-03")
def test_api_get_session_returns_full_payload(api_test_client):
    client, sets_root = api_test_client
    _seed_api_session(sets_root)
    resp = client.get("/api/sets/s1")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["session_id"] == "s1"
    assert payload["manifest"]["capture_device"] == "BlackHole 2ch"
    assert len(payload["segments"]) == 1


@pytest.mark.requirement("SET-03")
def test_api_get_session_404_for_missing(api_test_client):
    client, _ = api_test_client
    resp = client.get("/api/sets/nope")
    assert resp.status_code == 404


@pytest.mark.requirement("SET-03")
def test_api_timeline_streams_ndjson(api_test_client):
    client, sets_root = api_test_client
    _seed_api_session(sets_root)
    resp = client.get("/api/sets/s1/timeline")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    lines = [line for line in resp.text.splitlines() if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[1])["action"] == "track_loaded"


@pytest.mark.requirement("SET-03")
def test_api_transitions_returns_classified(api_test_client):
    client, sets_root = api_test_client
    _seed_api_session(sets_root)
    resp = client.get("/api/sets/s1/transitions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["predicted_class"] == "cut"


@pytest.mark.requirement("SET-03")
def test_api_relabel_appends_to_labels_jsonl(api_test_client):
    client, sets_root = api_test_client
    _seed_api_session(sets_root)
    resp = client.post(
        "/api/sets/s1/transitions/0/label",
        json={"class": "blend", "labeler": "web_ui"},
    )
    assert resp.status_code == 200
    labels_file = sets_root / "s1" / "labels.jsonl"
    assert labels_file.exists()
    row = json.loads(labels_file.read_text().strip().splitlines()[-1])
    assert row["class"] == "blend"
    assert row["labeler"] == "web_ui"


@pytest.mark.requirement("SET-03")
def test_api_relabel_rejects_unknown_class(api_test_client):
    client, sets_root = api_test_client
    _seed_api_session(sets_root)
    resp = client.post(
        "/api/sets/s1/transitions/0/label",
        json={"class": "nonsense"},
    )
    assert resp.status_code == 400


@pytest.mark.requirement("SET-03")
def test_api_audio_allowed_on_localhost_when_private(api_test_client):
    client, sets_root = api_test_client
    _seed_api_session(sets_root, private=True)
    resp = client.get("/api/sets/s1/audio/audio_2026-04-17T21-30-00.mp3")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/mpeg")


@pytest.mark.requirement("SET-03")
def test_api_audio_forbidden_on_non_localhost_when_private(
    sets_root: Path, monkeypatch
):
    """Directly drive ASGI with a non-local client to hit the 403 path."""
    monkeypatch.setattr(sets_paths_mod, "SETS_DIR", sets_root)
    _seed_api_session(sets_root, private=True)
    # Build a standalone app for a clean client override
    app = FastAPI()
    app.include_router(api_mod.router)
    # TestClient uses host='testclient' by default which our allow-list
    # treats as localhost. Override the allow-list to a strict {"127.0.0.1"}
    # temporarily so the TestClient request is rejected.
    monkeypatch.setattr(
        api_mod, "_LOCALHOST_HOSTS", frozenset({"127.0.0.1"})
    )
    with TestClient(app) as client:
        resp = client.get("/api/sets/s1/audio/audio_2026-04-17T21-30-00.mp3")
    assert resp.status_code == 403


@pytest.mark.requirement("SET-03")
def test_api_audio_rejects_non_audio_name(api_test_client):
    """Any segment that doesn't match ``audio_*.mp3`` is 400'd by the helper."""
    client, sets_root = api_test_client
    _seed_api_session(sets_root)
    # A filename without the audio_ prefix is rejected by resolve_segment_path.
    resp = client.get("/api/sets/s1/audio/manifest.json.mp3")
    assert resp.status_code == 400


@pytest.mark.requirement("SET-03")
def test_api_audio_404_for_missing_segment(api_test_client):
    client, sets_root = api_test_client
    _seed_api_session(sets_root)
    resp = client.get("/api/sets/s1/audio/audio_missing.mp3")
    assert resp.status_code == 404


@pytest.mark.requirement("SET-03")
def test_api_audio_rejects_session_id_traversal(api_test_client):
    """Regression for SECURITY-RED-TEAM finding 1 (HIGH).

    A crafted session_id with encoded traversal sequences must never reach
    a FileResponse, even when a manifest.json exists in the traversed dir.
    """
    client, sets_root = api_test_client
    _seed_api_session(sets_root)
    # Craft a sibling dir that contains both a manifest.json and a valid
    # audio_*.mp3 outside SETS_DIR; this is what would let the bug turn into
    # local file disclosure if resolve_segment_path were bypassed.
    outside = sets_root.parent / "outside-sets"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "manifest.json").write_text("{}", encoding="utf-8")
    (outside / "audio_2026-04-17T21-30-00.mp3").write_bytes(b"\xff\xfb" + b"\x00" * 512)
    # Percent-encoded ..%2F path still must not return a 200 FileResponse.
    resp = client.get(
        "/api/sets/..%2F..%2Fetc%2Fpasswd/audio/audio_2026-04-17T21-30-00.mp3"
    )
    assert resp.status_code != 200
    assert resp.status_code in {400, 404}
