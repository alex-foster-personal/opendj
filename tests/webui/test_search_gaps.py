"""Zero-result search gap list: GET /api/v1/search records, GET /api/v1/library/search-gaps reads (LIBM-12321).

Real state.db built with the shared writer (fixtures from test_search.py), real
FTS index, real sidecar store under tmp_path.

Regression lines:
  - [if] a non-empty search returns zero tracks [then] it is logged once, [else stop]
  - [if] the same zero-result search repeats [then] one row with a higher count, [else stop]
  - [if] a search returns tracks or is blank [then] nothing is logged, [else stop]
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server import search_gaps_store
from tests.webui.test_search import fixture_db, search_client

__all__ = ["fixture_db", "search_client"]

pytestmark = pytest.mark.requirement("LIBM-12321")


def _gaps(client: TestClient) -> list[dict[str, object]]:
    """Read the gap list through the library route."""
    resp = client.get("/api/v1/library/search-gaps")
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def test_zero_result_search_is_logged(search_client: TestClient) -> None:
    """[if] a non-empty search returns zero tracks [then] the gap list names it, [else stop]."""
    assert search_client.get("/api/v1/search", params={"q": "Zzyzx Nowhere"}).json()["total"] == 0
    gaps = _gaps(search_client)
    assert [g["normalized_query"] for g in gaps] == ["zzyzx nowhere"]
    assert gaps[0]["query"] == "Zzyzx Nowhere"
    assert gaps[0]["hit_count"] == 1


def test_repeated_zero_result_search_is_deduplicated(search_client: TestClient) -> None:
    """[if] the same zero-result search repeats in another case/spacing [then] one row, count 3, [else stop]."""
    for q in ("zzyzx", "ZZYZX", "  zzyzx  "):
        search_client.get("/api/v1/search", params={"q": q})
    gaps = _gaps(search_client)
    assert len(gaps) == 1
    assert gaps[0]["hit_count"] == 3


def test_successful_and_blank_searches_are_not_logged(search_client: TestClient, fixture_db: Path) -> None:
    """[if] a search returns tracks or is blank [then] the gap list stays empty and no store is made, [else stop]."""
    assert search_client.get("/api/v1/search", params={"q": "Lantern"}).json()["total"] >= 1
    search_client.get("/api/v1/search", params={"q": "   "})
    assert _gaps(search_client) == []
    assert not search_gaps_store.store_path(fixture_db).exists()


def test_gap_list_orders_most_recent_first(tmp_path: Path) -> None:
    """[if] two gaps were searched at different times [then] the later one is listed first, [else stop]."""
    db = tmp_path / "state.db"
    search_gaps_store.record_zero_result("older", db, now="2026-11-12T10:00:00.000000Z")
    search_gaps_store.record_zero_result("newer", db, now="2026-11-13T10:00:00.000000Z")
    assert [g.query for g in search_gaps_store.list_gaps(db)] == ["newer", "older"]
