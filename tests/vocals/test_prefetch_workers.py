"""PERFMODE-01 vocals prefetch worker scaler."""

from __future__ import annotations

import pytest

from apps.shared.perf_tier import PerfTier, background_worker_count
from apps.vocals.prefetch import DEFAULT_WORKERS


@pytest.mark.requirement("PERFMODE-01")
def test_low_tier_halves_default_workers() -> None:
    assert background_worker_count(DEFAULT_WORKERS, PerfTier.LOW) == 8
    assert background_worker_count(DEFAULT_WORKERS, PerfTier.STANDARD) == 16
