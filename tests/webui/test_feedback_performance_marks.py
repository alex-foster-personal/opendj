"""Real route and disk regressions for durable performance-feedback marks."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from apps.webui.server.app import create_app


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
