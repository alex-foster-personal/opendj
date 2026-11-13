"""LATENCY-04 live-stems HTTP plan: deck_count gating keeps BPM validation.

Regression lines:
- [if] a supported deck_count carries an invalid BPM [then] HTTP still refuses
  with the named BPM 409 instead of serializing a plan
- [if] an unsupported deck_count carries an invalid BPM [then] the deck_count
  refusal wins, matching plan_live_stems check order
- [if] a supported deck_count carries a valid BPM [then] HTTP returns the full
  plan shape for that deck count
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.stems.live_capability import (
    BENCHMARK_MIN_HASHES_PER_SECOND,
    LIVE_STEMS_CAPABILITY_PATH,
    SUPPORTED_DECK_COUNTS,
    assess_machine,
)
from apps.stems.live_capability_api import router

INVALID_BPMS: tuple[tuple[str, str], ...] = (
    ("0", "BPM must be between"),
    ("-120", "BPM must be between"),
    ("1000", "BPM must be between"),
    ("nan", "BPM must be finite"),
    ("inf", "BPM must be finite"),
)


def _client() -> TestClient:
    """Build a TestClient over the live-capability router with an enabled M3 Pro capability."""
    app = FastAPI()
    app.state.live_stems_capability = assess_machine("Apple M3 Pro", BENCHMARK_MIN_HASHES_PER_SECOND)
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


@pytest.mark.requirement("LATENCY-04")
@pytest.mark.parametrize("deck_count", sorted(SUPPORTED_DECK_COUNTS))
@pytest.mark.parametrize(("bpm", "error_prefix"), INVALID_BPMS)
def test_supported_deck_count_with_invalid_bpm_is_a_bpm_refusal(deck_count: int, bpm: str, error_prefix: str) -> None:
    """[if] deck_count is 2 or 4 but BPM is invalid [then] 409 BPM refusal, no plan, [else stop]."""
    with _client() as client:
        response = client.get(
            LIVE_STEMS_CAPABILITY_PATH,
            params={"deck_count": deck_count, "bpm": bpm},
        )

    assert response.status_code == 409
    body = response.json()
    assert isinstance(body["detail"], str)
    assert body["detail"].startswith(error_prefix)
    assert "deck_count" not in body["detail"]
    assert "plan" not in body


@pytest.mark.requirement("LATENCY-04")
@pytest.mark.parametrize("deck_count", [0, 1, 3, 5])
@pytest.mark.parametrize(("bpm", "_error_prefix"), INVALID_BPMS)
def test_unsupported_deck_count_with_invalid_bpm_names_deck_count_first(
    deck_count: int, bpm: str, _error_prefix: str
) -> None:
    """[if] deck_count and BPM are both invalid [then] 409 names deck_count, not BPM, [else stop]."""
    with _client() as client:
        response = client.get(
            LIVE_STEMS_CAPABILITY_PATH,
            params={"deck_count": deck_count, "bpm": bpm},
        )

    assert response.status_code == 409
    body = response.json()
    detail = body["detail"]
    assert isinstance(detail, str)
    assert detail == (f"live stems supports deck_count {sorted(SUPPORTED_DECK_COUNTS)}, got {deck_count}")
    assert "BPM" not in detail
    assert "plan" not in body


@pytest.mark.requirement("LATENCY-04")
@pytest.mark.parametrize(
    ("deck_count", "bpm", "lookahead_bars", "lookahead_ms"),
    [
        (2, "20", 4, 48_000),
        (2, "128", 4, 7_500),
        (2, "400", 4, 2_400),
        (4, "20", 8, 96_000),
        (4, "128", 8, 15_000),
        (4, "400", 8, 4_800),
    ],
)
def test_supported_deck_count_with_valid_bpm_returns_full_plan(
    deck_count: int, bpm: str, lookahead_bars: int, lookahead_ms: int
) -> None:
    """[if] deck_count is 2 or 4 and BPM is valid [then] 200 with the full plan shape, [else stop]."""
    with _client() as client:
        response = client.get(
            LIVE_STEMS_CAPABILITY_PATH,
            params={"deck_count": deck_count, "bpm": bpm},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    plan = body["plan"]
    assert set(plan) == {
        "deck_count",
        "bpm",
        "lookahead_bars",
        "lookahead_ms",
        "working_set_ceiling_mib",
    }
    assert plan["deck_count"] == deck_count
    assert plan["bpm"] == float(bpm)
    assert plan["lookahead_bars"] == lookahead_bars
    assert plan["lookahead_ms"] == lookahead_ms
    assert plan["working_set_ceiling_mib"] == 1024
