"""POST /api/v1/vocals/{stable_id}/validate-correction (issue #13173).

- [if] a comment names a real vocal area [then] validate-correction accepts it with a canonical anchor, [else stop].
- [if] a comment names an unknown vocal area [then] refusal carries code vocal_area_unknown, [else stop].
- [if] a correction payload has invalid bounds [then] refusal carries code vocal_bounds_invalid, [else stop].
- [if] path stable_id disagrees with the anchor id [then] refusal carries code vocal_stable_id_mismatch, [else stop].
- [if] click time equals a region end_s [then] half-open matching excludes that region, [else stop].
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.webui.server.vocal_anchor_validation import validate_vocal_anchor_for_vocals
from tests.webui.test_vocals_routes import client  # noqa: F401  (pytest fixture)

pytestmark = pytest.mark.requirement("TRACKEDIT-13449")
pytest_plugins = ["tests.webui.test_vocals_routes"]


def _post_validate(client: TestClient, stable_id: str, anchor: str) -> tuple[int, dict]:
    response = client.post(
        f"/api/v1/vocals/{stable_id}/validate-correction",
        json={"anchor": anchor},
    )
    body = response.json()
    if isinstance(body.get("detail"), dict):
        return response.status_code, body["detail"]
    return response.status_code, body


def test_validate_accepts_region_anchor_for_real_pvdi_area(client: TestClient) -> None:
    status = client.get("/api/v1/vocals/pvdi/status")
    assert status.status_code == 200, status.text
    region = status.json()["regions"][0]
    start_s = region["start_s"]
    end_s = region["end_s"]
    anchor = f"vocal-area:pvdi:{start_s:.2f}-{end_s:.2f}:rekordbox"
    code, body = _post_validate(client, "pvdi", anchor)
    assert code == 200, body
    assert "code" not in body
    assert body["kind"] == "region"
    assert body["status"] == "rekordbox"
    assert body["anchor"] == anchor
    assert body["start_s"] == pytest.approx(start_s, abs=0.01)
    assert body["end_s"] == pytest.approx(end_s, abs=0.01)
    assert body["intensity"] == region["intensity"]


def test_validate_at_anchor_resolves_region_bounds(client: TestClient) -> None:
    status = client.get("/api/v1/vocals/pvdi/status")
    assert status.status_code == 200, status.text
    region = status.json()["regions"][0]
    mid_s = (region["start_s"] + region["end_s"]) / 2.0
    anchor = f"vocal-area:pvdi@{mid_s:.2f}:rekordbox"
    code, body = _post_validate(client, "pvdi", anchor)
    assert code == 200, body
    assert body["kind"] == "region"
    assert body["start_s"] == pytest.approx(region["start_s"], abs=0.01)
    assert body["end_s"] == pytest.approx(region["end_s"], abs=0.01)
    assert body["time_s"] == pytest.approx(mid_s, abs=0.01)


def test_validate_unknown_region_returns_named_code(client: TestClient) -> None:
    code, body = _post_validate(client, "pvdi", "vocal-area:pvdi:9.99-10.99:rekordbox")
    assert code == 422, body
    assert body["code"] == "vocal_area_unknown"


def test_validate_at_time_outside_regions_is_unknown(client: TestClient) -> None:
    code, body = _post_validate(client, "pvdi", "vocal-area:pvdi@99.00:rekordbox")
    assert code == 422, body
    assert body["code"] == "vocal_area_unknown"


def test_validate_inverted_bounds_returns_named_code(client: TestClient) -> None:
    code, body = _post_validate(client, "pvdi", "vocal-area:pvdi:2.00-1.00:rekordbox")
    assert code == 422, body
    assert body["code"] == "vocal_bounds_invalid"


def test_validate_equal_bounds_returns_named_code(client: TestClient) -> None:
    code, body = _post_validate(client, "pvdi", "vocal-area:pvdi:1.00-1.00:rekordbox")
    assert code == 422, body
    assert body["code"] == "vocal_bounds_invalid"


def test_validate_malformed_anchor_returns_named_code(client: TestClient) -> None:
    code, body = _post_validate(client, "pvdi", "not-a-vocal-anchor")
    assert code == 422, body
    assert body["code"] == "vocal_anchor_invalid"


def test_validate_stale_status_returns_named_code(client: TestClient) -> None:
    status = client.get("/api/v1/vocals/pvdi/status")
    region = status.json()["regions"][0]
    anchor = f"vocal-area:pvdi:{region['start_s']:.2f}-{region['end_s']:.2f}:demucs"
    code, body = _post_validate(client, "pvdi", anchor)
    assert code == 422, body
    assert body["code"] == "vocal_status_stale"


def test_validate_stable_id_mismatch_returns_named_code(client: TestClient) -> None:
    status = client.get("/api/v1/vocals/pvdi/status")
    region = status.json()["regions"][0]
    anchor = f"vocal-area:cached:{region['start_s']:.2f}-{region['end_s']:.2f}:rekordbox"
    code, body = _post_validate(client, "pvdi", anchor)
    assert code == 422, body
    assert body["code"] == "vocal_stable_id_mismatch"


def test_validate_not_analyzed_returns_named_code(client: TestClient) -> None:
    code, body = _post_validate(client, "noanlz", "vocal-area:noanlz@1.00:not_analyzed")
    assert code == 422, body
    assert body["code"] == "vocal_not_analyzed"


def test_validate_no_vocals_returns_named_code() -> None:
    vocals = {"status": "no_vocals", "fps": 21.53, "regions": []}
    with pytest.raises(HTTPException) as exc_info:
        validate_vocal_anchor_for_vocals(
            "track-a",
            "vocal-area:track-a@0.50:no_vocals",
            vocals,
        )
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "vocal_area_unavailable"


def test_half_open_boundary_at_region_end_is_unknown(client: TestClient) -> None:
    status = client.get("/api/v1/vocals/pvdi/status")
    region = status.json()["regions"][0]
    end_s = region["end_s"]
    anchor = f"vocal-area:pvdi@{end_s:.2f}:rekordbox"
    code, body = _post_validate(client, "pvdi", anchor)
    assert code == 422, body
    assert body["code"] == "vocal_area_unknown"
