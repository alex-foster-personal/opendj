"""HTTP acceptance tests for the Open DJ deck-observation ingest.

[if] snapshots are posted with no live recorder [then ⛔️] they are accepted.
[if] a track is audible past the threshold [then ⛔️] the set has no row for it.
[if] a snapshot is malformed [then ⛔️] it is quietly dropped instead of 422.
[if] the recorder never enabled the source [then ⛔️] the post looks like it worked.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sets.api import router
from apps.sets.recorder_service import RecorderService
from apps.sets.sources.opendj_source import ADVISORY_DWELL_S
from apps.sets.state import SetsState

SESSION_ID = "2026-08-31T20-00-00"
T0 = datetime(2026, 8, 31, 20, 0, 0, tzinfo=UTC)
CADENCE_S = 2.0


@pytest.fixture
def observer_client(tmp_path: Path):
    service = RecorderService(
        sets_root=tmp_path / "sets",
        db_path=tmp_path / "sets" / "sets.db",
        capture_enabled=False,
    )
    app = FastAPI()
    app.state.sets_recorder_service = service
    app.include_router(router)
    with TestClient(app) as client:
        yield client, service


def _deck(stable_id, *, audible: bool, playing: bool = True) -> dict:
    return {
        "stable_id": stable_id,
        "playing": playing,
        "audible": audible,
        "position_ms": 0.0,
        "duration_ms": 240_000.0,
        "title": "Test Track",
        "artist": "Test Artist",
    }


def _run(span_s: float, decks: dict) -> list[dict]:
    out: list[dict] = []
    offset = 0.0
    while offset <= span_s:
        out.append(
            {
                "observed_at": (T0 + timedelta(seconds=offset)).isoformat(
                    timespec="milliseconds"
                ),
                "decks": decks,
            }
        )
        offset += CADENCE_S
    return out


def _start(client: TestClient, sources: list[str]) -> None:
    resp = client.post(
        "/api/sets/recorder/start",
        json={
            "session_id": SESSION_ID,
            "ffmpeg_device_idx": 0,
            "sources": sources,
        },
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.requirement("SET-01")
def test_posting_observations_without_a_live_recorder_is_409(observer_client):
    client, _ = observer_client
    resp = client.post(
        "/api/sets/deck-observations",
        json={"snapshots": _run(0, {"1": _deck("trk-a", audible=True)})},
    )
    assert resp.status_code == 409
    assert "no HTTP-owned recorder is active" in resp.json()["detail"]


@pytest.mark.requirement("SET-01")
def test_recorder_without_the_source_rejects_observations(observer_client):
    client, _ = observer_client
    _start(client, [])
    resp = client.post(
        "/api/sets/deck-observations",
        json={"snapshots": _run(0, {"1": _deck("trk-a", audible=True)})},
    )
    assert resp.status_code == 409
    assert "without the 'opendj_decks' source" in resp.json()["detail"]
    client.post(f"/api/sets/recorder/{SESSION_ID}/stop")


@pytest.mark.requirement("SET-01")
def test_audible_run_posted_over_http_lands_in_the_set(observer_client):
    """The whole point: a track played on OUR deck ends up in the set."""
    client, service = observer_client
    _start(client, ["opendj_decks"])

    posted = client.post(
        "/api/sets/deck-observations",
        json={
            "snapshots": _run(
                ADVISORY_DWELL_S + 4, {"1": _deck("trk-a", audible=True)}
            )
        },
    )
    assert posted.status_code == 202, posted.text

    # The recorder's own poll thread drains the queue; force one drain so
    # the test does not race the 500 ms cadence.
    service.active_source("opendj_decks").poll_once()

    status = client.get("/api/sets/deck-observations")
    assert status.status_code == 200
    deck_one = status.json()["decks"]["1"]
    assert deck_one["stable_id"] == "trk-a"
    assert deck_one["recorded"] is True

    client.post(f"/api/sets/recorder/{SESSION_ID}/stop")

    state = SetsState(db_path=service.db_path)
    rows = state.fetch_events(SESSION_ID, action="track_loaded")
    assert [r.track_stable_id for r in rows] == ["trk-a"]
    assert rows[0].deck == "1"
    assert rows[0].source == "opendj_decks"
    stamp = datetime.fromisoformat(rows[0].wall_clock)
    assert stamp.utcoffset() == timedelta(0)


@pytest.mark.requirement("SET-01")
def test_loaded_but_silent_run_posted_over_http_records_nothing(observer_client):
    client, service = observer_client
    _start(client, ["opendj_decks"])

    client.post(
        "/api/sets/deck-observations",
        json={
            "snapshots": _run(
                ADVISORY_DWELL_S * 3, {"1": _deck("trk-a", audible=False)}
            )
        },
    )
    service.active_source("opendj_decks").poll_once()
    client.post(f"/api/sets/recorder/{SESSION_ID}/stop")

    state = SetsState(db_path=service.db_path)
    assert state.fetch_events(SESSION_ID, action="track_loaded") == []


@pytest.mark.requirement("SET-01")
@pytest.mark.parametrize(
    "bad_deck, expect",
    [
        ({"9": {"stable_id": "a", "playing": True, "audible": True,
                "position_ms": 0}}, "unknown deck"),
        ({"1": {"stable_id": "a", "playing": True, "audible": "yes",
                "position_ms": 0}}, "bool"),
        ({"1": {"stable_id": None, "playing": True, "audible": True,
                "position_ms": 0}}, "audible"),
        ({"1": {"stable_id": "a", "playing": True, "position_ms": 0}},
         "missing audible"),
    ],
)
def test_malformed_snapshots_are_422_not_silently_dropped(
    observer_client, bad_deck, expect
):
    client, _ = observer_client
    _start(client, ["opendj_decks"])
    resp = client.post(
        "/api/sets/deck-observations",
        json={
            "snapshots": [
                {"observed_at": T0.isoformat(timespec="milliseconds"),
                 "decks": bad_deck}
            ]
        },
    )
    assert resp.status_code == 422, resp.text
    assert expect in resp.json()["detail"]
    client.post(f"/api/sets/recorder/{SESSION_ID}/stop")


@pytest.mark.requirement("SET-01")
def test_empty_batch_is_rejected(observer_client):
    client, _ = observer_client
    _start(client, ["opendj_decks"])
    resp = client.post("/api/sets/deck-observations", json={"snapshots": []})
    assert resp.status_code == 422
    client.post(f"/api/sets/recorder/{SESSION_ID}/stop")
