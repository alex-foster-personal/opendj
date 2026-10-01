"""POST /library/preview-strips: rows in view catch up without a click (NATIVE-21).

Regression lines:
  - if a mapped row's strip is not its ANLZ strip then broken
  - if an unmapped row's cached strip is not returned then broken
  - if an unmapped row with nothing on disk is not null, pending and bumped then broken
  - if a mapped row with no strip is bumped or reported pending then broken
  - if more than 200 ids are accepted then broken
  - if an engine without the drain reports pending ids then broken

[if] the batch read disagrees with a listing row or skips the bump [then] fail, [else stop].
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server.rb_vendor_pkg import track_rows
from apps.webui.server.rb_vendor_pkg.row_assets import RowAssets
from apps.webui.server.routes import library

pytestmark = pytest.mark.requirement("NATIVE-21")

MAPPED = "m" * 40
MAPPED_BARE = "b" * 40
UNMAPPED_CACHED = "c" * 40
UNMAPPED_MISSING = "u" * 40


class FakeDrain:
    def __init__(self) -> None:
        self.bumped: list[str] = []

    def bump(self, stable_id: str) -> None:
        self.bumped.append(stable_id)


def _assets(b64: str | None, peak: int | None) -> RowAssets:
    return RowAssets(
        preview_b64=b64, preview_max=peak, artwork_available=None,
        artwork_status="unknown", pvdi_vocals={"status": "not_analyzed"},
    )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Two mapped rows (one with an ANLZ strip), two unmapped (one cached)."""
    metas = {MAPPED: object(), MAPPED_BARE: object()}
    monkeypatch.setattr(track_rows, "bulk_rb_meta", lambda ids: {i: metas[i] for i in ids if i in metas})
    monkeypatch.setattr(
        track_rows, "bulk_rb_row_assets",
        lambda m, *, resolver: {MAPPED: _assets("QU5MWg==", 31), MAPPED_BARE: _assets(None, None)},
    )
    cached = {UNMAPPED_CACHED: ("TE9DQUw=", 17)}
    monkeypatch.setattr(track_rows, "local_preview_strip", lambda sid: cached.get(sid, (None, None)))
    drain = FakeDrain()
    app = FastAPI()
    app.include_router(library.router, prefix="/api/v1")
    app.state.ahead_analysis = drain
    return {"client": TestClient(app), "drain": drain, "app": app}


def _post(client: TestClient, ids: list[str]) -> Any:
    return client.post("/api/v1/library/preview-strips", json={"ids": ids})


def test_each_row_reads_what_its_listing_row_reads(world: dict[str, Any]) -> None:
    """[if] a batch row differs from its listing-row source [then] fail, [else stop]."""
    body = _post(world["client"], [MAPPED, MAPPED_BARE, UNMAPPED_CACHED, UNMAPPED_MISSING]).json()
    assert body["strips"][MAPPED] == {"preview_b64": "QU5MWg==", "preview_max": 31}
    assert body["strips"][UNMAPPED_CACHED] == {"preview_b64": "TE9DQUw=", "preview_max": 17}
    assert body["strips"][MAPPED_BARE] is None
    assert body["strips"][UNMAPPED_MISSING] is None


def test_a_missing_unmapped_strip_is_pending_and_bumped(world: dict[str, Any]) -> None:
    """[if] a missing unmapped strip is not bumped and pending [then] fail, [else stop]."""
    body = _post(world["client"], [MAPPED_BARE, UNMAPPED_CACHED, UNMAPPED_MISSING]).json()
    assert body["pending"] == [UNMAPPED_MISSING]
    assert world["drain"].bumped == [UNMAPPED_MISSING], "a mapped row with no ANLZ is never drain work"


def test_without_the_drain_nothing_is_pending(world: dict[str, Any]) -> None:
    """[if] an unarmed engine reports pending strips [then] fail, [else stop]."""
    world["app"].state.ahead_analysis = None
    body = _post(world["client"], [UNMAPPED_MISSING]).json()
    assert body == {"strips": {UNMAPPED_MISSING: None}, "pending": []}


def test_the_batch_is_capped(world: dict[str, Any]) -> None:
    """[if] more than the cap or zero ids are accepted [then] fail, [else stop]."""
    cap = library.MAX_PREVIEW_STRIP_IDS
    assert _post(world["client"], [UNMAPPED_MISSING] * cap).status_code == 200
    assert _post(world["client"], [UNMAPPED_MISSING] * (cap + 1)).status_code == 422
    assert _post(world["client"], []).status_code == 422
