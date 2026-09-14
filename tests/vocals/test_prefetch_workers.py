"""PERFMODE-01 vocals prefetch worker scaler; PERFMODE-03 posture composition."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared.app_posture import AppPosture, apply_posture_to_workers
from apps.shared.perf_tier import PerfTier, background_worker_count
from apps.vocals.prefetch import DEFAULT_WORKERS, read_ahead


@pytest.mark.requirement("PERFMODE-01")
def test_low_tier_halves_default_workers() -> None:
    """[if] tier is LOW [then] background_worker_count halves defaults to 8, [else stop]."""
    assert background_worker_count(DEFAULT_WORKERS, PerfTier.LOW) == 8
    assert background_worker_count(DEFAULT_WORKERS, PerfTier.STANDARD) == 16


@pytest.mark.requirement("PERFMODE-03")
def test_standard_prep_keeps_tier_workers() -> None:
    """[if] posture is Prep on STANDARD tier [then] workers stay 16, [else stop]."""
    count = background_worker_count(DEFAULT_WORKERS, PerfTier.STANDARD)
    assert apply_posture_to_workers(count, AppPosture.PREP) == 16


@pytest.mark.requirement("PERFMODE-03")
def test_standard_gig_halves_workers() -> None:
    """[if] posture is Gig on STANDARD tier [then] workers halve to 8, [else stop]."""
    count = background_worker_count(DEFAULT_WORKERS, PerfTier.STANDARD)
    assert apply_posture_to_workers(count, AppPosture.GIG) == 8


@pytest.mark.requirement("PERFMODE-03")
def test_low_gig_halves_to_four() -> None:
    """[if] posture is Gig on LOW tier [then] workers halve to 4, [else stop]."""
    count = background_worker_count(DEFAULT_WORKERS, PerfTier.LOW)
    assert apply_posture_to_workers(count, AppPosture.GIG) == 4


@pytest.mark.requirement("PERFMODE-03")
def test_explicit_workers_ignores_posture(tmp_path: Path) -> None:
    """[if] read_ahead passes workers explicitly [then] posture is not applied, [else stop]."""
    path = tmp_path / "track.bin"
    path.write_bytes(b"x")
    items = list(read_ahead([path], workers=3, depth=1))
    assert len(items) == 1
