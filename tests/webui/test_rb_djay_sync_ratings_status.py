"""Ratings diff summary on the rb-djay sync status payload (issue #12987).

[if] GET /rb-djay-sync/status is called [then] it reports pending ratings-diff rows by resolution without a write, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sync import apply_ratings, djay_sync_service
from apps.webui.server.routes import rb_djay_sync

pytestmark = pytest.mark.requirement("PLATFORM-12987")

_HEADER = "rb_content_id,djay_uuid,field,rb_value,djay_value,resolution\n"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Mount the rb-djay router with SYNC_DIR redirected and ratings writes trapped."""
    monkeypatch.setattr(djay_sync_service, "SYNC_DIR", tmp_path)

    def _no_write(*_args: object, **_kwargs: object) -> None:
        """Fail the test if status ever reaches the live ratings writer."""
        raise AssertionError("status must not invoke a ratings write")

    monkeypatch.setattr(apply_ratings, "live_run", _no_write)
    app = FastAPI()
    app.include_router(rb_djay_sync.router, prefix="/api/v1")
    return TestClient(app)


def _ratings(client: TestClient) -> dict:
    """Fetch the ratings_diff block from the status route."""
    resp = client.get("/api/v1/rb-djay-sync/status")
    assert resp.status_code == 200
    return resp.json()["ratings_diff"]


def test_status_ratings_diff_absent(client: TestClient) -> None:
    """No ratings-diff.csv reports present=false with zero counts and no error."""
    assert _ratings(client) == {
        "present": False,
        "row_count": 0,
        "by_resolution": {},
        "error": None,
    }


def test_status_ratings_diff_counts_by_resolution(client: TestClient, tmp_path: Path) -> None:
    """Accepted and conflict rows are counted per resolution and the CSV is left untouched."""
    csv_path = tmp_path / "ratings-diff.csv"
    body = (
        _HEADER
        + "1,u1,rating,3,0,accept_rb\n"
        + "2,u2,rating,0,4,accept_djay\n"
        + "3,u3,rating,2,5,conflict\n"
        + "4,u4,rating,1,3,conflict\n"
    )
    csv_path.write_text(body, encoding="utf-8")
    summary = _ratings(client)
    assert summary == {
        "present": True,
        "row_count": 4,
        "by_resolution": {"accept_rb": 1, "accept_djay": 1, "conflict": 2},
        "error": None,
    }
    assert csv_path.read_text(encoding="utf-8") == body


@pytest.mark.parametrize(
    ("content", "needle"),
    [
        (b"rb_content_id,djay_uuid,rating\n1,u1,3\n", b"no resolution column"),
        (_HEADER.encode() + b"1,u1,rating,3,0,conflict,surplus\n", b"line 2 has extra fields"),
        (_HEADER.encode() + b"1,u1,rating,3,0,\xff\xfe\n", b"unreadable"),
    ],
)
def test_status_ratings_diff_malformed(client: TestClient, tmp_path: Path, content: bytes, needle: bytes) -> None:
    """A malformed ratings-diff.csv keeps status 200 and names the problem in error."""
    (tmp_path / "ratings-diff.csv").write_bytes(content)
    summary = _ratings(client)
    assert summary["present"] is True
    assert summary["row_count"] == 0
    assert summary["by_resolution"] == {}
    assert needle.decode() in summary["error"]
