"""HTTP rescue snapshot ring list/restore windows (RESCUE-04)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.engine_core.app import create_app
from apps.engine_core.config import EngineConfig, apply_env_contract, prepare_layout
from apps.engine_core.rescue.store import RescueStore
from apps.engine_core.rescue_api import RESTORE_PATH, SNAPSHOTS_PATH

pytestmark = pytest.mark.requirement("RESCUE-04")


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    (root / "state").mkdir(parents=True)
    monkeypatch.setenv("MDT_DATA_DIR", str(root))
    monkeypatch.setenv("MDT_LIBRARY_MODE", "local")
    apply_env_contract(EngineConfig(data_dir=root, host="127.0.0.1", port=8787))
    prepare_layout(EngineConfig(data_dir=root, host="127.0.0.1", port=8787))
    return root


@pytest.fixture
def client(data_dir: Path) -> TestClient:
    app = create_app(EngineConfig(data_dir=data_dir, host="127.0.0.1", port=8787))
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        test_client.data_dir = data_dir  # type: ignore[attr-defined]
        yield test_client


def _deck_snapshot(*, stable_id: str | None, playing: bool) -> dict:
    return {
        "deck_id": 1,
        "stable_id": stable_id,
        "source_path": None,
        "playing": playing,
        "position_ms": 0,
        "beat_stamp": {"kind": "sample", "position_ms": 0},
        "pitch": 1.0,
        "pitch_range": 8,
        "master_tempo_enabled": True,
        "key_sync_enabled": False,
        "quantize_enabled": True,
        "beat_sync_enabled": True,
        "sync_mode": "bar",
        "is_master": False,
        "cue_ms": None,
        "loop": None,
        "hot_cue_armed": None,
        "stems": {
            "vocal": {"muted": False, "solo": False, "gain": 0.5},
            "instrumental": {"muted": False, "solo": False, "gain": 0.5},
            "drums": {"muted": False, "solo": False, "gain": 0.5},
        },
        "mixer_channel": {
            "trim": 0.5,
            "eq_high": 0.5,
            "eq_mid": 0.5,
            "eq_low": 0.5,
            "filter": 0.5,
            "fader": 1.0,
            "assign": "THRU",
            "cue_enabled": False,
        },
    }


def _empty_payload(*, deck1_playing: bool = False, deck1_stable_id: str | None = "a" * 40) -> dict:
    decks = {}
    for deck_id in (1, 2, 3, 4):
        deck = _deck_snapshot(stable_id=None, playing=False)
        deck["deck_id"] = deck_id
        if deck_id == 1:
            deck["stable_id"] = deck1_stable_id
            deck["playing"] = deck1_playing
        decks[deck_id] = deck
    return {
        "schema": 1,
        "captured_at_ms": 0,
        "reason": "periodic",
        "app_posture": "gig",
        "master_deck": None,
        "playlist_id": "pl-test",
        "deck_layout": "more",
        "decks": decks,
        "mixer": {
            "crossfader": 0.5,
            "master": 0.8,
            "headphones": {
                "mix": 0.5,
                "level": 0.5,
                "output_mode": "practice",
                "selected_master_output_device_id": None,
                "selected_output_device_id": None,
            },
        },
    }


def _seed_snapshot(
    data_dir: Path,
    *,
    age_ms: int,
    deck1_playing: bool = False,
    deck1_stable_id: str | None = "a" * 40,
) -> str:
    now_ms = int(time.time() * 1000)
    payload = _empty_payload(deck1_playing=deck1_playing, deck1_stable_id=deck1_stable_id)
    store = RescueStore(data_dir)
    entry = store.append(captured_at_ms=now_ms - age_ms, payload=payload)
    return entry.id


def test_empty_ring_lists_no_snapshots(client: TestClient) -> None:
    response = client.get(SNAPSHOTS_PATH)
    assert response.status_code == 200
    assert response.json() == {"snapshots": []}


def test_seed_three_hour_snapshot_lists_age(client: TestClient, data_dir: Path) -> None:
    snapshot_id = _seed_snapshot(data_dir, age_ms=3 * 60 * 60 * 1000)
    response = client.get(SNAPSHOTS_PATH)
    assert response.status_code == 200
    body = response.json()
    assert len(body["snapshots"]) == 1
    row = body["snapshots"][0]
    assert row["id"] == snapshot_id
    assert row["id"].startswith("slot-")
    assert row["age_ms"] >= 3 * 60 * 60 * 1000 - 5_000
    assert row["deck_count_loaded"] == 1


def test_layout_restore_three_hours_never_resumes(client: TestClient, data_dir: Path) -> None:
    _seed_snapshot(data_dir, age_ms=3 * 60 * 60 * 1000, deck1_playing=True)
    response = client.post(RESTORE_PATH, json={"play": False})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "layout"
    assert all(
        deck["outcome"] in {"paused", "missing"} for deck in body["decks"].values()
    )
    assert "resumed" not in {deck["outcome"] for deck in body["decks"].values()}


def test_play_restore_three_hours_refused(client: TestClient, data_dir: Path) -> None:
    _seed_snapshot(data_dir, age_ms=3 * 60 * 60 * 1000)
    response = client.post(RESTORE_PATH, json={"play": True})
    assert response.status_code == 422
    assert "10 min" in response.json()["detail"]


def test_play_restore_five_minutes_reports_resumed(client: TestClient, data_dir: Path) -> None:
    _seed_snapshot(data_dir, age_ms=5 * 60 * 1000, deck1_playing=True)
    response = client.post(RESTORE_PATH, json={"play": True})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "play"
    assert body["decks"]["1"]["outcome"] == "resumed"


def test_stale_snapshot_refused(client: TestClient, data_dir: Path) -> None:
    _seed_snapshot(data_dir, age_ms=25 * 60 * 60 * 1000)
    response = client.post(RESTORE_PATH, json={"play": False})
    assert response.status_code == 422
    assert "24 h" in response.json()["detail"]
