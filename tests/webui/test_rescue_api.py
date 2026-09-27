"""HTTP rescue snapshot ring list/restore windows (RESCUE-04).

[if] the ring holds a snapshot [then] the list reports its id, age and loaded decks, [else stop].
[if] a layout restore runs [then] no deck resumes, even one that was playing, [else stop].
[if] a play restore is under 10 min old [then] the playing deck resumes, [else stop].
[if] a play restore is older than 10 min [then] it is refused with 422, [else stop].
[if] a layout restore is older than 24 h [then] it is refused with 422, [else stop].
[if] a deck's track is soft-deleted in the library [then] restore reports it missing, [else stop].
[if] a deck's track is live in the library [then] a play restore still resumes it, [else stop].
[if] every library track is soft-deleted [then] restore reports the deck missing, [else stop].
[if] the library cannot be read [then] restore fails with 503, never loadable decks, [else stop].
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.engine_core.app import create_app
from apps.engine_core.config import EngineConfig, apply_env_contract, prepare_layout
from apps.engine_core.rescue.store import RescueStore
from apps.engine_core.rescue_api import RESTORE_PATH, SNAPSHOTS_PATH, _present_stable_ids
from apps.shared.state import db as state_db

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


def _ensure_library_schema(data_dir: Path) -> None:
    """Apply state migrations before the engine opens a read-only library probe."""
    conn = state_db.open_rw(data_dir / "state" / "state.db")
    conn.close()


@pytest.fixture
def client(data_dir: Path) -> TestClient:
    _ensure_library_schema(data_dir)
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


def test_play_restore_five_minutes_reports_resumed(data_dir: Path) -> None:
    body = _restore_with_library(data_dir, library={"a" * 40: False})
    assert body["mode"] == "play"
    assert body["decks"]["1"]["outcome"] == "resumed"


def test_stale_snapshot_refused(client: TestClient, data_dir: Path) -> None:
    _seed_snapshot(data_dir, age_ms=25 * 60 * 60 * 1000)
    response = client.post(RESTORE_PATH, json={"play": False})
    assert response.status_code == 422
    assert "24 h" in response.json()["detail"]


def _seed_library_track(data_dir: Path, stable_id: str, *, deleted: bool) -> None:
    conn = state_db.open_rw(data_dir / "state" / "state.db")
    try:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, file_path, "
            "created_at, updated_at, deleted_at) VALUES (?, 'inferred', ?, ?, "
            "'2026-09-09T00:00:00Z', '2026-09-09T00:00:00Z', ?)",
            (
                stable_id,
                stable_id,
                str(data_dir / f"{stable_id}.wav"),
                "2026-09-10T00:00:00Z" if deleted else None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _restore_with_library(data_dir: Path, *, library: dict[str, bool]) -> dict:
    """Seed `library` ({stable_id: deleted}), then play-restore a snapshot whose
    deck 1 holds "a" * 40."""
    # Seed the library before the engine starts: its lifespan creates the state
    # schema, and a writer racing that creation sees a half-built database.
    if not library:
        _ensure_library_schema(data_dir)
    for stable_id, deleted in library.items():
        _seed_library_track(data_dir, stable_id, deleted=deleted)
    _seed_snapshot(data_dir, age_ms=5 * 60 * 1000, deck1_playing=True)
    app = create_app(EngineConfig(data_dir=data_dir, host="127.0.0.1", port=8787))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(RESTORE_PATH, json={"play": True})
    assert response.status_code == 200
    return response.json()


def test_restore_reports_a_soft_deleted_track_missing(data_dir: Path) -> None:
    body = _restore_with_library(data_dir, library={"b" * 40: False, "a" * 40: True})
    assert body["decks"]["1"]["outcome"] == "missing", (
        "if a soft-deleted track restores as loadable, then rescue reloads a track "
        "the user deleted"
    )


def test_play_restore_resumes_a_live_library_track(data_dir: Path) -> None:
    body = _restore_with_library(data_dir, library={"b" * 40: False, "a" * 40: False})
    assert body["decks"]["1"]["outcome"] == "resumed", (
        "if a live library track restores as missing, then the deleted-row filter "
        "hides tracks it should keep"
    )


def test_a_library_of_only_tombstones_restores_the_deck_missing(data_dir: Path) -> None:
    body = _restore_with_library(data_dir, library={"a" * 40: True})
    assert body["decks"]["1"]["outcome"] == "missing", (
        "if an all-deleted library reads as unknown, then rescue reloads every "
        "track the user deleted"
    )


def test_an_unreadable_library_is_unknown_and_an_all_deleted_one_is_empty(
    tmp_path: Path, data_dir: Path
) -> None:
    # Route behavior for unreadable library is tested in
    # test_unreadable_library_restore_fails_with_503.
    assert _present_stable_ids(tmp_path / "no-such-data-dir") is None, (
        "if a missing library reads as empty, then every deck restores missing"
    )
    _seed_library_track(data_dir, "a" * 40, deleted=True)
    assert _present_stable_ids(data_dir) == set(), (
        "if an all-deleted library reads as unknown, then deleted tracks reload"
    )


def test_unreadable_library_restore_fails_with_503(data_dir: Path) -> None:
    _seed_snapshot(data_dir, age_ms=5 * 60 * 1000, deck1_playing=True)
    app = create_app(EngineConfig(data_dir=data_dir, host="127.0.0.1", port=8787))
    # Corrupted AFTER boot: an engine refuses to boot on an unreadable
    # state.db (GUARD-09), and since #3965 it serves its own data dir's store,
    # so the case rescue must survive is a library that goes bad at runtime.
    (data_dir / "state" / "state.db").write_bytes(b"not-a-sqlite-db")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(RESTORE_PATH, json={"play": True})
    assert response.status_code == 503
    assert "library_unreadable" in response.json()["detail"].lower()


def test_restore_with_empty_library_reports_missing(data_dir: Path) -> None:
    body = _restore_with_library(data_dir, library={})
    assert body["decks"]["1"]["outcome"] == "missing"
