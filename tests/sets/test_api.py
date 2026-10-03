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
from apps.sets.share import SetShareConfig, SetShareError
from apps.webui.server.app import create_app
from apps.webui.server.share_gate import (
    ACCESS_EMAIL_HEADER,
    ACCESS_JWT_HEADER,
    AUTH_CLOUDFLARE_ACCESS,
    AUTH_TOKEN,
    ShareConfig,
)


@pytest.fixture
def api_test_client(sets_root: Path, monkeypatch):
    """A FastAPI TestClient pointing at a temp SETS_DIR."""
    monkeypatch.setattr(sets_paths_mod, "SETS_DIR", sets_root)
    app = FastAPI()
    app.include_router(api_mod.router)
    with TestClient(app) as client:
        yield client, sets_root


@pytest.fixture
def set_share_client(sets_root: Path):
    """Real application boundary configured for a Cloudflare Access share host."""
    app = create_app(
        mount_frontend=False,
        share_config=ShareConfig(
            host="sets.example.test",
            auth=AUTH_CLOUDFLARE_ACCESS,
        ),
        set_share_config=SetShareConfig("https://sets.example.test"),
        sets_root=sets_root,
    )
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
            mp3_segments=[
                AudioSegment(
                    name=mp3.name,
                    start_t_s=0.0,
                    duration_s=300.0,
                    size_bytes=mp3.stat().st_size,
                )
            ],
        ),
    )
    # timeline.jsonl
    timeline = [
        {
            "session_id": session_id,
            "timestamp_s": 0.0,
            "wall_clock": "2026-04-17T21:30:00+00:00",
            "action": "session_start",
            "source": "recorder",
            "deck": None,
            "track_stable_id": None,
            "value": {},
        },
        {
            "session_id": session_id,
            "timestamp_s": 5.0,
            "wall_clock": "2026-04-17T21:30:05+00:00",
            "action": "track_loaded",
            "source": "djay_monitor",
            "deck": "A",
            "track_stable_id": "uuid-1",
            "value": {"title": "Track 1"},
        },
    ]
    (sess / "timeline.jsonl").write_text("\n".join(json.dumps(t) for t in timeline) + "\n")
    # transitions.jsonl
    transitions = [
        {
            "idx": 0,
            "t_start_s": 0.0,
            "t_change_s": 10.0,
            "t_end_s": 12.0,
            "from_deck": "A",
            "to_deck": "B",
            "from_track": "t1",
            "to_track": "t2",
            "predicted_class": "cut",
            "confidence": 0.9,
            "model_version": "rules-v0",
            "features": {
                "overlap_s": 0.2,
                "fade_s": 0.1,
                "incoming_preload_s": 1.0,
                "outgoing_trail_s": 0.1,
                "time_since_prev_transition_s": 0.0,
                "is_same_deck_reload": 0.0,
            },
        },
    ]
    (sess / "transitions.jsonl").write_text("\n".join(json.dumps(t) for t in transitions) + "\n")
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
@pytest.mark.parametrize(
    ("encoded_session_id", "expected_status"),
    [("..%5Coutside", 400), ("..%2Foutside", 404)],
)
def test_api_timeline_rejects_encoded_session_path_traversal(
    api_test_client, encoded_session_id: str, expected_status: int
) -> None:
    """Decoded separators are client errors, never out-of-tree timeline reads."""
    client, sets_root = api_test_client
    outside = sets_root.parent / "outside"
    outside.mkdir()
    (outside / "manifest.json").write_text("{}", encoding="utf-8")
    (outside / "timeline.jsonl").write_text('{"outside":true}\n', encoding="utf-8")

    response = client.get(f"/api/sets/{encoded_session_id}/timeline")

    assert response.status_code == expected_status
    if expected_status == 400:
        assert "session_id" in response.json()["detail"]


@pytest.mark.requirement("SET-03")
def test_api_relabel_rejects_encoded_backslash_without_writing_outside_root(
    api_test_client,
) -> None:
    """The relabel endpoint must not append labels.jsonl through ``..\\``."""
    client, sets_root = api_test_client
    outside = sets_root.parent / "outside"
    outside.mkdir()
    write_manifest(
        outside,
        Manifest(
            session_id="outside",
            started_at="2026-04-17T21:30:00+00:00",
            ended_at=None,
            capture_device="test",
            event_count=0,
        ),
    )

    response = client.post(
        "/api/sets/..%5Coutside/transitions/0/label",
        json={"class": "blend"},
    )

    assert response.status_code == 400
    assert not (outside / "labels.jsonl").exists()


@pytest.mark.requirement("SET-03")
def test_api_timeline_rejects_encoded_nul_as_client_error(api_test_client) -> None:
    """NUL must not escape the resolver as an internal server error."""
    client, _ = api_test_client

    response = client.get("/api/sets/bad%00id/timeline")

    assert response.status_code == 400
    assert "session_id" in response.json()["detail"]


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


@pytest.mark.requirement("SET-11")
def test_api_audio_serves_an_odj_audio_wav_segment_as_wav(api_test_client):
    """[if] the segment came from odj-audio's capture [then] it is served as audio/wav."""
    client, sets_root = api_test_client
    sess = _seed_api_session(sets_root, private=True)
    (sess / "audio_2026-04-17T21-35-00.wav").write_bytes(b"RIFF" + b"\x00" * 40)
    resp = client.get("/api/sets/s1/audio/audio_2026-04-17T21-35-00.wav")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "audio/wav"


@pytest.mark.requirement("SET-03")
def test_api_audio_allowed_on_localhost_when_private(api_test_client):
    client, sets_root = api_test_client
    _seed_api_session(sets_root, private=True)
    resp = client.get("/api/sets/s1/audio/audio_2026-04-17T21-30-00.mp3")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/mpeg")


@pytest.mark.requirement("SET-03")
def test_api_audio_forbidden_on_non_localhost_when_private(sets_root: Path, monkeypatch):
    """Directly drive ASGI with a non-local client to hit the 403 path."""
    monkeypatch.setattr(sets_paths_mod, "SETS_DIR", sets_root)
    _seed_api_session(sets_root, private=True)
    # Build a standalone app for a clean client override
    app = FastAPI()
    app.include_router(api_mod.router)
    with TestClient(app, client=("203.0.113.1", 50000)) as client:
        resp = client.get("/api/sets/s1/audio/audio_2026-04-17T21-30-00.mp3")
    assert resp.status_code == 403


@pytest.mark.requirement("SET-08")
def test_api_metadata_share_requires_explicit_consent_and_https_host(
    set_share_client,
) -> None:
    """A share link cannot turn on from a private set or an unsafe host."""
    client, sets_root = set_share_client
    session_dir = _seed_api_session(sets_root)

    missing_consent = client.post("/api/sets/s1/share", json={})
    assert missing_consent.status_code == 422
    assert json.loads((session_dir / "manifest.json").read_text())["share_state"] == "private"

    with pytest.raises(SetShareError):
        SetShareConfig("http://sets.example.test")
    with pytest.raises(SetShareError):
        SetShareConfig("https://sets.example.test:not-a-port")
    with pytest.raises(SetShareError):
        SetShareConfig("https://sets.example.test:99999")
    assert json.loads((session_dir / "manifest.json").read_text())["share_state"] == "private"


@pytest.mark.requirement("SET-08")
def test_api_metadata_share_publishes_real_set_without_audio(set_share_client) -> None:
    """A finalized set gets one HTTPS shared-view URL and no remote MP3 route."""
    client, sets_root = set_share_client
    _seed_api_session(sets_root)

    published = client.post("/api/sets/s1/share", json={"confirm_metadata_only": True})

    assert published.status_code == 200
    assert published.json() == {
        "session_id": "s1",
        "share_state": "shared_cloud",
        "share_url": "https://sets.example.test/sets/shared/s1",
        "content": "metadata_only",
    }
    fetched = client.get("/api/sets/s1/share")
    assert fetched.status_code == 200
    assert fetched.json() == published.json()

    audio = client.get(
        "/api/sets/s1/audio/audio_2026-04-17T21-30-00.mp3",
        headers={
            "host": "sets.example.test",
            ACCESS_EMAIL_HEADER: "friend@example.test",
            ACCESS_JWT_HEADER: "signed-access-assertion",
        },
    )
    assert audio.status_code == 404


@pytest.mark.requirement("SET-08")
def test_api_share_audience_can_only_read_published_metadata(set_share_client) -> None:
    """A share host cannot enumerate or fetch a private set by guessing its ID."""
    client, sets_root = set_share_client
    _seed_api_session(sets_root)

    share_headers = {
        "host": "sets.example.test",
        ACCESS_EMAIL_HEADER: "friend@example.test",
        ACCESS_JWT_HEADER: "signed-access-assertion",
    }
    assert client.get("/api/sets", headers=share_headers).json() == []
    assert client.get("/api/sets/s1", headers=share_headers).status_code == 404
    assert client.get("/api/sets/s1/timeline", headers=share_headers).status_code == 404
    assert client.get("/api/sets/s1/transitions", headers=share_headers).status_code == 404

    published = client.post("/api/sets/s1/share", json={"confirm_metadata_only": True})
    assert published.status_code == 200
    assert client.get("/api/sets", headers=share_headers).json()[0]["session_id"] == "s1"
    shared_session = client.get("/api/sets/s1", headers=share_headers)
    assert shared_session.status_code == 200
    assert set(shared_session.json()) == {"summary"}
    assert "BlackHole 2ch" not in shared_session.text
    assert "audio_2026-04-17T21-30-00.mp3" not in shared_session.text
    timeline_path = sets_root / "s1" / "timeline.jsonl"
    with timeline_path.open("a", encoding="utf-8") as timeline:
        timeline.write(
            json.dumps(
                {
                    "action": "source_error",
                    "timestamp_s": 8.0,
                    "value": {"diagnostic": "/private/recordings/djay.db"},
                }
            )
            + "\n"
        )
        timeline.write('{"action":"track_loaded"\n')
    shared_timeline = client.get("/api/sets/s1/timeline", headers=share_headers)
    assert shared_timeline.status_code == 200
    assert "/private/recordings/djay.db" not in shared_timeline.text
    assert [json.loads(line)["action"] for line in shared_timeline.text.splitlines()] == [
        "track_loaded"
    ]
    shared_page = client.get("/sets/shared/s1", headers=share_headers)
    assert shared_page.status_code == 200
    assert "OPEN DJ SET HISTORY" in shared_page.text
    assert "audio_2026-04-17T21-30-00.mp3" not in shared_page.text
    audio = client.get("/api/sets/s1/audio/audio_2026-04-17T21-30-00.mp3", headers=share_headers)
    assert audio.status_code == 404


@pytest.mark.requirement("SET-08")
def test_api_metadata_share_requires_matching_access_host(set_share_client) -> None:
    """A set link needs the same configured origin as the share gate."""
    _client, sets_root = set_share_client
    _seed_api_session(sets_root)
    app = create_app(
        mount_frontend=False,
        share_config=ShareConfig(host="access.example.test", auth=AUTH_CLOUDFLARE_ACCESS),
        set_share_config=SetShareConfig("https://sets.example.test"),
        sets_root=sets_root,
    )
    with TestClient(app) as client:
        response = client.post("/api/sets/s1/share", json={"confirm_metadata_only": True})
    assert response.status_code == 409


@pytest.mark.requirement("SET-08")
def test_api_metadata_share_rejects_implicit_token_authentication(set_share_client) -> None:
    """A configured token mode never emits a browser link without its token."""
    _client, sets_root = set_share_client
    _seed_api_session(sets_root)
    app = create_app(
        mount_frontend=False,
        share_config=ShareConfig(
            host="sets.example.test",
            auth=AUTH_TOKEN,
            token="secret-token",
        ),
        set_share_config=SetShareConfig("https://sets.example.test"),
        sets_root=sets_root,
    )
    with TestClient(app) as client:
        response = client.post("/api/sets/s1/share", json={"confirm_metadata_only": True})
    assert response.status_code == 409


@pytest.mark.requirement("SET-08")
def test_api_share_audience_cannot_publish_when_global_read_only_is_disabled(
    set_share_client,
) -> None:
    """A share recipient cannot publish guessed private set identifiers."""
    _client, sets_root = set_share_client
    session_dir = _seed_api_session(sets_root)
    app = create_app(
        mount_frontend=False,
        share_config=ShareConfig(
            host="sets.example.test",
            auth=AUTH_CLOUDFLARE_ACCESS,
            read_only=False,
        ),
        set_share_config=SetShareConfig("https://sets.example.test"),
        sets_root=sets_root,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/sets/s1/share",
            json={"confirm_metadata_only": True},
            headers={
                "host": "sets.example.test",
                ACCESS_EMAIL_HEADER: "friend@example.test",
                ACCESS_JWT_HEADER: "signed-access-assertion",
            },
        )
    assert response.status_code == 403
    assert json.loads((session_dir / "manifest.json").read_text())["share_state"] == "private"


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
    resp = client.get("/api/sets/..%2F..%2Fetc%2Fpasswd/audio/audio_2026-04-17T21-30-00.mp3")
    assert resp.status_code != 200
    assert resp.status_code in {400, 404}
