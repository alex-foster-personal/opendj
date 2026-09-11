"""PERFMODE tier floor for local stems (issue #1866)."""

from __future__ import annotations

from apps.engine_core.perf_tier import (
    LOCAL_STEMS_MIN_TIER,
    PerfTier,
    local_stems_tier_refusal,
    meets_floor,
)


def test_local_stems_requires_standard_tier() -> None:
    assert LOCAL_STEMS_MIN_TIER == PerfTier.STANDARD


def test_low_tier_is_refused() -> None:
    refusal = local_stems_tier_refusal(PerfTier.LOW)
    assert refusal is not None
    assert "STANDARD" in refusal
    assert "LOW" in refusal


def test_standard_tier_is_allowed() -> None:
    assert local_stems_tier_refusal(PerfTier.STANDARD) is None
    assert meets_floor(LOCAL_STEMS_MIN_TIER, PerfTier.HIGH)
