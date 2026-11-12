"""Measured live-generation RSS stays within the locked quality ceiling."""

from __future__ import annotations

from typing import cast

import pytest

from apps.stems.live_capability import LiveStemsCapabilityError, LiveStemsQuality
from apps.stems.live_plan import evaluate_live_generation_rss


@pytest.mark.requirement("RB-PERFORMANCE-12829")
@pytest.mark.parametrize(
    ("quality", "rss_mib", "expected"),
    [
        (LiveStemsQuality.LOW, 0, True),
        (LiveStemsQuality.LOW, 511.5, True),
        (LiveStemsQuality.LOW, 512, True),
        (LiveStemsQuality.LOW, 512.0001, False),
        (LiveStemsQuality.LOW, 513, False),
        (LiveStemsQuality.LOW, 1024, False),
        (LiveStemsQuality.HIGH, 0, True),
        (LiveStemsQuality.HIGH, 512.0001, True),
        (LiveStemsQuality.HIGH, 1023.5, True),
        (LiveStemsQuality.HIGH, 1024, True),
        (LiveStemsQuality.HIGH, 1024.0001, False),
        (LiveStemsQuality.HIGH, 1025, False),
    ],
)
def test_rss_ceiling_is_inclusive(quality: LiveStemsQuality, rss_mib: float, expected: bool) -> None:
    """[if] enabled quality has measured RSS [then] only RSS at or below passes, [else stop]."""
    assert evaluate_live_generation_rss(quality, working_set_rss_mib=rss_mib) is expected


@pytest.mark.requirement("RB-PERFORMANCE-12829")
@pytest.mark.parametrize("rss_mib", [0, 512, 1024, 1025])
def test_disabled_quality_refuses_measurements(rss_mib: float) -> None:
    """[if] disabled quality supplies RSS [then] evaluation raises a policy error, [else stop]."""
    with pytest.raises(LiveStemsCapabilityError, match="disabled"):
        evaluate_live_generation_rss(LiveStemsQuality.DISABLED, working_set_rss_mib=rss_mib)


@pytest.mark.requirement("RB-PERFORMANCE-12829")
@pytest.mark.parametrize("quality", [LiveStemsQuality.LOW, LiveStemsQuality.HIGH])
@pytest.mark.parametrize("rss_mib", [-1, -0.001, float("nan"), float("inf"), float("-inf"), True, False, None, "512"])
def test_invalid_measurements_raise(quality: LiveStemsQuality, rss_mib: object) -> None:
    """[if] RSS is invalid [then] evaluation raises an explicit MiB input error, [else stop]."""
    with pytest.raises(LiveStemsCapabilityError, match="MiB"):
        evaluate_live_generation_rss(quality, working_set_rss_mib=cast(float, rss_mib))


@pytest.mark.requirement("RB-PERFORMANCE-12829")
@pytest.mark.parametrize("quality", ["low", "high", "unsupported", None, 0])
def test_invalid_quality_raises(quality: object) -> None:
    """[if] quality is invalid [then] its error identifies the quality and MiB, [else stop]."""
    with pytest.raises(LiveStemsCapabilityError, match=r"quality.*MiB") as error:
        evaluate_live_generation_rss(cast(LiveStemsQuality, quality), working_set_rss_mib=512)
    assert repr(quality) in str(error.value)


@pytest.mark.requirement("RB-PERFORMANCE-12829")
def test_missing_measurement_is_not_a_default_success() -> None:
    """[if] RSS is missing [then] the required argument raises TypeError, [else stop]."""
    with pytest.raises(TypeError, match="working_set_rss_mib"):
        evaluate_live_generation_rss(LiveStemsQuality.LOW)  # type: ignore[call-arg]
