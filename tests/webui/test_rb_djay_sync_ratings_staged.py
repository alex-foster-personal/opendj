"""HTTP ratings apply enforces the CLI staged safety contract (issue #12972).

[if] the HTTP ratings apply route is called [then] it follows the CLI dry-run, cautious, bulk sequence, [else stop].
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sync import apply_ratings, djay_sync_service, safety
from apps.webui.server.routes import rb_djay_sync

pytestmark = pytest.mark.requirement("PLATFORM-12972")

ROUTE = "/api/v1/rb-djay-sync/ratings/apply"


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Build a client with the write gate open, a temp stamp dir and a fake writer."""
    csv_path = tmp_path / "ratings-diff.csv"
    csv_path.write_text(
        "rb_content_id,djay_uuid,field,rb_value,djay_value,resolution,action_hint\n3,u3,rating,5,3,rb_wins,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(safety, "_ROLLOUT_STAMP_DIR", tmp_path / "rollout")
    monkeypatch.setattr(rb_djay_sync, "require_writeback_enabled", lambda _s: None)
    monkeypatch.setattr(djay_sync_service, "require_writeback_enabled", lambda _s: None)
    calls: list[set[str] | None] = []

    def _fake_live_run(rows: list[dict], **kwargs: Any) -> int:
        """Record the track filter instead of writing any library."""
        calls.append(kwargs.get("only_tracks"))
        return 0

    monkeypatch.setattr(apply_ratings, "live_run", _fake_live_run)
    app = FastAPI()
    app.include_router(rb_djay_sync.router, prefix="/api/v1")
    return {
        "client": TestClient(app),
        "calls": calls,
        "csv": str(csv_path),
        "stamp": tmp_path / "rollout" / "apply_ratings.cautious-ok",
    }


def _live(harness: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Return a live ratings apply body with the risk flag set."""
    return {
        "dry_run": False,
        "live": True,
        "i_understand_the_risks": True,
        "diff_csv": harness["csv"],
        **extra,
    }


def test_bulk_without_cautious_success_is_refused(harness: dict[str, Any]) -> None:
    """[if] an HTTP bulk ratings apply has no recorded cautious success [then] 409 SAFETY_ABORT and no write, [else stop]."""
    resp = harness["client"].post(ROUTE, json=_live(harness, bulk=True))
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "SAFETY_ABORT"
    assert harness["calls"] == []


def test_cautious_selected_tracks_records_success_then_bulk_runs(
    harness: dict[str, Any],
) -> None:
    """[if] an HTTP cautious ratings apply names selected tracks [then] only they are written and a later bulk runs, [else stop]."""
    client = harness["client"]
    resp = client.post(ROUTE, json=_live(harness, cautious=True, tracks=["3"]))
    assert resp.status_code == 200
    assert resp.json()["stage"] == "cautious"
    assert harness["calls"] == [{"3"}]
    assert harness["stamp"].exists()
    bulk = client.post(ROUTE, json=_live(harness, bulk=True))
    assert bulk.status_code == 200
    assert bulk.json()["stage"] == "bulk"
    assert harness["calls"] == [{"3"}, None]


@pytest.mark.parametrize(
    "extra",
    [{}, {"cautious": True}, {"cautious": True, "bulk": True, "tracks": ["3"]}],
)
def test_live_without_a_valid_stage_is_refused(harness: dict[str, Any], extra: dict[str, Any]) -> None:
    """[if] a live ratings apply names no stage, a cautious stage without tracks, or both stages [then] 409 SAFETY_ABORT and no write, [else stop]."""
    resp = harness["client"].post(ROUTE, json=_live(harness, **extra))
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "SAFETY_ABORT"
    assert harness["calls"] == []
    assert not harness["stamp"].exists()


def test_dry_run_writes_nothing_and_records_no_stage(harness: dict[str, Any]) -> None:
    """[if] an HTTP dry run requests ratings apply [then] a preview returns, nothing is written and no cautious stamp is recorded, [else stop]."""
    resp = harness["client"].post(ROUTE, json={"dry_run": True, "diff_csv": harness["csv"], "bulk": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["row_count"] == 1
    assert harness["calls"] == []
    assert not harness["stamp"].exists()
