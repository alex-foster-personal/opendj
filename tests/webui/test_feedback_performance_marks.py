"""Real route and disk regressions for durable performance-feedback marks.

[if] a performance feedback mark is posted [then] it is written to disk and readable back, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app

pytestmark = pytest.mark.requirement("PREF-02")


def _app(data_dir: Path) -> TestClient:
    app = create_app(mount_frontend=False)
    app.state.data_dir = data_dir
    return TestClient(app)


def _mark(recorded_at_ms: int, vote: str = "good") -> dict:
    return {
        "recorded_at_ms": recorded_at_ms,
        "vote": vote,
        "decks": [
            {
                "deck_id": 1,
                "stable_id": "real-stable-id",
                "playing": True,
                "audible": True,
                "position_ms": 1234.5,
                "loop": {"in_ms": 1000.0, "out_ms": 2000.0},
            }
        ],
        "mixer": {
            "crossfader": 0.0,
            "master": 0.8,
            "channels": [
                {
                    "deck_id": 1,
                    "trim": 1.0,
                    "eq_low": 0.0,
                    "eq_mid": 0.0,
                    "eq_high": 0.0,
                    "filter": 0.5,
                    "fader": 1.0,
                    "assign": "thru",
                }
            ],
        },
    }


def test_performance_marks_survive_a_new_app_and_return_detached_snapshots(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    with _app(data_dir) as first:
        created = first.post("/api/v1/feedback/performance-marks", json=_mark(1))
        assert created.status_code == 201
        returned = created.json()
        returned["last_mark"]["decks"][0]["stable_id"] = "mutated-response"

    with _app(data_dir) as restarted:
        persisted = restarted.get("/api/v1/feedback/performance-marks")
        assert persisted.status_code == 200
        assert persisted.json()["count"] == 1
        assert persisted.json()["last_mark"]["decks"][0]["stable_id"] == "real-stable-id"


def test_performance_marks_preserve_a_non_default_filter_value(tmp_path: Path) -> None:
    """Issue #990 follow-up (PR #1021, discussion_r3966901255): the frontend
    started sending FILTER on every mark once the dial became real audio, but
    MixerChannelMarkOut had no `filter` field, so pydantic silently dropped it
    on ingest and a mark could no longer reproduce the mix the user judged."""
    mark = _mark(1)
    mark["mixer"]["channels"][0]["filter"] = 0.85
    with _app(tmp_path / "data") as client:
        created = client.post("/api/v1/feedback/performance-marks", json=mark)
        assert created.status_code == 201
        assert created.json()["last_mark"]["mixer"]["channels"][0]["filter"] == 0.85

        persisted = client.get("/api/v1/feedback/performance-marks")
        assert persisted.json()["last_mark"]["mixer"]["channels"][0]["filter"] == 0.85


def test_performance_marks_read_a_pre_990_record_missing_filter(tmp_path: Path) -> None:
    """Issue #990 follow-up (PR #1021, discussion_r3970221216): MixerChannelMarkOut
    made `filter` a required field, so a mark written by the previous release (no
    `filter` key at all) would fail pydantic validation on every future GET and
    POST, locking a user out of both their old history and recording a new mark.
    `filter` must default to the neutral 0.5 dead-zone value for such records."""
    data_dir = tmp_path / "data"
    feedback_dir = data_dir / "feedback"
    feedback_dir.mkdir(parents=True)
    legacy_mark = _mark(1)
    del legacy_mark["mixer"]["channels"][0]["filter"]
    (feedback_dir / "performance-marks.json").write_text(
        json.dumps({"marks": [legacy_mark]}), encoding="utf-8"
    )

    with _app(data_dir) as client:
        read_back = client.get("/api/v1/feedback/performance-marks")
        assert read_back.status_code == 200
        assert read_back.json()["last_mark"]["mixer"]["channels"][0]["filter"] == 0.5

        created = client.post("/api/v1/feedback/performance-marks", json=_mark(2))
        assert created.status_code == 201
        assert created.json()["count"] == 2


def test_performance_marks_round_trip_eq_and_loop(tmp_path: Path) -> None:
    mark = _mark(1)
    mark["mixer"]["channels"][0]["eq_low"] = 0.2
    mark["mixer"]["channels"][0]["eq_mid"] = 0.4
    mark["mixer"]["channels"][0]["eq_high"] = 0.8
    with _app(tmp_path / "data") as client:
        created = client.post("/api/v1/feedback/performance-marks", json=mark)
        assert created.status_code == 201
        last = created.json()["last_mark"]
        channel = last["mixer"]["channels"][0]
        assert channel["eq_low"] == 0.2
        assert channel["eq_mid"] == 0.4
        assert channel["eq_high"] == 0.8
        assert last["decks"][0]["loop"] == {"in_ms": 1000.0, "out_ms": 2000.0}

        persisted = client.get("/api/v1/feedback/performance-marks").json()["last_mark"]
        assert persisted["mixer"]["channels"][0]["eq_low"] == 0.2
        assert persisted["mixer"]["channels"][0]["eq_mid"] == 0.4
        assert persisted["mixer"]["channels"][0]["eq_high"] == 0.8
        assert persisted["decks"][0]["loop"] == {"in_ms": 1000.0, "out_ms": 2000.0}


def test_performance_marks_keep_the_newest_four_hundred(tmp_path: Path) -> None:
    with _app(tmp_path / "data") as client:
        for timestamp in range(401):
            response = client.post(
                "/api/v1/feedback/performance-marks", json=_mark(timestamp)
            )
            assert response.status_code == 201
        summary = client.get("/api/v1/feedback/performance-marks").json()
    assert summary["count"] == 400
    assert summary["last_mark"]["recorded_at_ms"] == 400
