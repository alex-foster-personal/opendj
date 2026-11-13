"""HTTP error mapping for ratings safety refusals on the rb-djay sync apply route (issue #12974).

[if] a ratings apply request hits a safety refusal [then] a typed status and stable code, never an unhandled server error, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sync import apply_ratings, safety
from apps.webui.server.routes import rb_djay_sync

pytestmark = pytest.mark.requirement("PLATFORM-12974")

_RATINGS_URL = "/api/v1/rb-djay-sync/ratings/apply"


@pytest.fixture
def client() -> TestClient:
    """Mount only the rb-djay sync router so failures surface as raw server errors."""
    app = FastAPI()
    app.include_router(rb_djay_sync.router, prefix="/api/v1")
    return TestClient(app)


def _empty_diff(tmp_path: Path) -> Path:
    """Write a header-only ratings diff CSV with no target rows."""
    csv_path = tmp_path / "ratings-diff.csv"
    csv_path.write_text("rb_content_id,djay_uuid,resolution\n", encoding="utf-8")
    return csv_path


def test_live_ratings_apply_while_rekordbox_running_returns_409_safety_abort(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rekordbox running during a live ratings apply maps to 409 SAFETY_ABORT.

    [if] the process rail reports Rekordbox running for a live ratings request [then] 409 with code SAFETY_ABORT and the rail message, [else stop].
    """
    monkeypatch.setenv("MDT_REKORDBOX_WRITEBACK_ENABLED", "1")
    monkeypatch.setattr(safety, "_is_running", lambda name: name == "Rekordbox")

    def _live_run(rows: list[dict], **kwargs: object) -> int:
        """Stand in for the writer by hitting the real Rekordbox process rail."""
        safety.assert_target_not_running("rekordbox")
        return 0

    monkeypatch.setattr(apply_ratings, "live_run", _live_run)
    resp = client.post(
        _RATINGS_URL,
        json={
            "dry_run": False,
            "live": True,
            "i_understand_the_risks": True,
            "cautious": True,
            "tracks": ["t-1"],
            "diff_csv": str(_empty_diff(tmp_path)),
        },
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "SAFETY_ABORT"
    assert "Rekordbox is running" in detail["message"]


def test_live_ratings_apply_without_acknowledgement_returns_422_live_write_refused(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live ratings apply without the risk acknowledgement is refused before any write.

    [if] i_understand_the_risks is absent on a live ratings request [then] 422 with code LIVE_WRITE_REFUSED and no writer call, [else stop].
    """
    monkeypatch.setenv("MDT_REKORDBOX_WRITEBACK_ENABLED", "1")
    calls: list[object] = []
    monkeypatch.setattr(apply_ratings, "live_run", lambda *a, **k: calls.append(a) or 0)
    resp = client.post(
        _RATINGS_URL,
        json={"dry_run": False, "live": True, "diff_csv": str(_empty_diff(tmp_path))},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "LIVE_WRITE_REFUSED"
    assert calls == []


def test_dry_run_ratings_apply_with_no_target_rows_returns_empty_summary(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry-run over a diff with no target rows answers 200 with a zero count.

    [if] a dry-run ratings diff has no target rows [then] 200 with dry_run true, row_count 0, an empty summary and no writer call, [else stop].
    """
    calls: list[object] = []
    monkeypatch.setattr(apply_ratings, "live_run", lambda *a, **k: calls.append(a) or 0)
    resp = client.post(_RATINGS_URL, json={"dry_run": True, "diff_csv": str(_empty_diff(tmp_path))})
    assert resp.status_code == 200
    assert resp.json() == {"dry_run": True, "row_count": 0, "summary": {}}
    assert calls == []
