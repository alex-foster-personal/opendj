"""Acceptance boundaries for live-stems lookahead planning and inference deadline policy.

Lookahead regression (issue #12436):
  if two decks at 120 BPM then four bars, 8000 ms, 24576000 PCM bytes else broken
  if four decks at 120 BPM then eight bars, 16000 ms, 98304000 PCM bytes else broken
  if unsupported deck count then LiveStemsPlanError else broken
  if non-finite or out-of-range BPM then LiveStemsPlanError else broken
"""

from __future__ import annotations

import math

import pytest

from apps.stems.live_capability import (
    BENCHMARK_MIN_HASHES_PER_SECOND,
    MAX_PLAN_BPM,
    MIN_PLAN_BPM,
    LiveStemsCapabilityError,
    LiveStemsPlan,
    assess_machine,
    plan_live_stems,
)
from apps.stems.live_plan import (
    LiveStemsPlanError,
    evaluate_live_stems_deadline,
    plan_live_stems_lookahead,
)


@pytest.fixture(params=[(2, 120, 8_000), (4, 120, 16_000), (2, 160, 6_000), (4, 160, 12_000)])
def plan(request: pytest.FixtureRequest) -> LiveStemsPlan:
    deck_count, bpm, expected_ms = request.param
    capability = assess_machine("Apple M3", BENCHMARK_MIN_HASHES_PER_SECOND)
    result = plan_live_stems(capability, deck_count=deck_count, bpm=bpm)
    assert result.lookahead_ms == expected_ms
    return result


@pytest.mark.requirement("RB-PERFORMANCE-12850")
@pytest.mark.parametrize("use_zero", [True, False])
def test_inference_strictly_before_deadline_is_enabled(plan: LiveStemsPlan, use_zero: bool) -> None:
    """[if] inference finishes strictly early [then] the deadline check enables, [else stop]."""
    inference_ms = 0.0 if use_zero else plan.lookahead_ms - 0.25
    original_plan = plan.to_dict()

    result = evaluate_live_stems_deadline(plan, inference_ms=inference_ms)

    assert result.enabled is True
    assert result.inference_ms == inference_ms
    assert result.lookahead_ms == plan.lookahead_ms
    assert f"{inference_ms} ms" in result.reason
    assert f"{plan.lookahead_ms} ms" in result.reason
    assert "strictly before" in result.reason
    assert plan.to_dict() == original_plan
    assert evaluate_live_stems_deadline(plan, inference_ms=inference_ms) == result


@pytest.mark.requirement("RB-PERFORMANCE-12850")
@pytest.mark.parametrize("overrun_ms", [0.0, 0.25], ids=["exact-deadline", "overrun"])
def test_inference_at_or_after_deadline_is_refused(plan: LiveStemsPlan, overrun_ms: float) -> None:
    """[if] inference meets or exceeds its deadline [then] the plan is refused, [else stop]."""
    inference_ms = plan.lookahead_ms + overrun_ms
    original_plan = plan.to_dict()

    result = evaluate_live_stems_deadline(plan, inference_ms=inference_ms)

    assert result.enabled is False
    assert result.inference_ms == inference_ms
    assert result.lookahead_ms == plan.lookahead_ms
    assert f"{inference_ms} ms" in result.reason
    assert f"{plan.lookahead_ms} ms" in result.reason
    assert "must finish strictly before" in result.reason
    assert plan.to_dict() == original_plan
    assert evaluate_live_stems_deadline(plan, inference_ms=inference_ms) == result


@pytest.mark.requirement("RB-PERFORMANCE-12850")
@pytest.mark.parametrize("inference_ms", [-0.25, math.nan, math.inf, -math.inf])
def test_invalid_inference_duration_raises(plan: LiveStemsPlan, inference_ms: float) -> None:
    """[if] inference duration is negative or non-finite [then] validation raises, [else stop]."""
    original_plan = plan.to_dict()

    with pytest.raises(LiveStemsCapabilityError, match="inference duration must be finite and nonnegative"):
        evaluate_live_stems_deadline(plan, inference_ms=inference_ms)

    assert plan.to_dict() == original_plan


@pytest.mark.requirement("RB-PERFORMANCE-12851")
def test_two_deck_plan_at_120_bpm() -> None:
    """[if] two decks at 120 BPM [then] four bars and locked ms/bytes, [else stop]."""
    plan = plan_live_stems_lookahead(deck_count=2, bpm=120)

    assert plan.lookahead_bars == 4
    assert plan.lookahead_ms == 8_000
    assert plan.lookahead_pcm_bytes == 24_576_000


@pytest.mark.requirement("RB-PERFORMANCE-12851")
def test_four_deck_plan_at_120_bpm() -> None:
    """[if] four decks at 120 BPM [then] eight bars and locked ms/bytes, [else stop]."""
    plan = plan_live_stems_lookahead(deck_count=4, bpm=120)

    assert plan.lookahead_bars == 8
    assert plan.lookahead_ms == 16_000
    assert plan.lookahead_pcm_bytes == 98_304_000


@pytest.mark.requirement("RB-PERFORMANCE-12851")
@pytest.mark.parametrize("deck_count", [0, 1, 3, 8])
def test_unsupported_deck_count_is_refused(deck_count: int) -> None:
    """[if] deck count is not 2 or 4 [then] LiveStemsPlanError names supported set, [else stop]."""
    with pytest.raises(LiveStemsPlanError, match="live stems supports deck_count"):
        plan_live_stems_lookahead(deck_count=deck_count, bpm=120)


@pytest.mark.requirement("RB-PERFORMANCE-12851")
@pytest.mark.parametrize("bpm", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_bpm_is_refused(bpm: float) -> None:
    """[if] BPM is non-finite [then] LiveStemsPlanError mentions finite BPM, [else stop]."""
    with pytest.raises(LiveStemsPlanError, match="BPM must be finite"):
        plan_live_stems_lookahead(deck_count=2, bpm=bpm)


@pytest.mark.requirement("RB-PERFORMANCE-12851")
@pytest.mark.parametrize("bpm", [0.0, -120.0])
def test_zero_or_negative_bpm_is_refused(bpm: float) -> None:
    """[if] BPM is zero or negative [then] LiveStemsPlanError cites bounds, [else stop]."""
    with pytest.raises(LiveStemsPlanError, match="BPM must be between"):
        plan_live_stems_lookahead(deck_count=2, bpm=bpm)


@pytest.mark.requirement("RB-PERFORMANCE-12851")
@pytest.mark.parametrize(
    ("bpm", "expected_ms"),
    [(MIN_PLAN_BPM, 48_000), (MAX_PLAN_BPM, 2_400)],
)
def test_bpm_range_edges_match_lookahead_formula(bpm: float, expected_ms: int) -> None:
    """[if] BPM is 20 or 400 [then] two-deck lookahead_ms matches formula, [else stop]."""
    plan = plan_live_stems_lookahead(deck_count=2, bpm=bpm)

    assert plan.lookahead_ms > 0
    assert plan.lookahead_ms == expected_ms
    assert plan.lookahead_ms == round(4 * 4 * 60_000 / bpm)


@pytest.mark.requirement("RB-PERFORMANCE-12851")
def test_lookahead_plan_to_dict_round_trips_fields() -> None:
    """[if] plan is serialized [then] to_dict preserves lookahead_pcm_bytes, [else stop]."""
    plan = plan_live_stems_lookahead(deck_count=2, bpm=120)
    data = plan.to_dict()

    assert data["lookahead_pcm_bytes"] == 24_576_000
    assert data["lookahead_ms"] == 8_000
    assert math.isfinite(float(data["bpm"]))
