"""B2BRELAY-02: the relay's clock estimator (skew-corrected median over min-RTT samples) and slewing.

Synthetic pings against a KNOWN peer clock (offset 1.234567 s, 50 ppm, the spike S1 injection),
so the error is exact. Each property carries a control that fails it when the mechanism is
removed, so a pass means the mechanism did the work.

- [if] pings carry a skewed, offset peer clock [then] the estimate tracks it within 0.1 ms, [else stop]
- [if] the estimate is more than 0.1 ms off on a clean path under 50 ppm skew [then] broken
- [if] removing the skew term (control) does NOT push the same run past 1 ms [then] the test cannot bite
- [if] one-sided queueing biases the estimate more than 2 ms (a plain median control is 5+ ms off) [then] broken
- [if] the slewed offset jumps on a small disagreement, overshoots it, or fails to step on a large one [then] broken
- [if] a ping received before it was sent is accepted [then] broken
"""

from __future__ import annotations

import random
import statistics

import pytest

from apps.b2b_sync import relay_clock
from apps.b2b_sync.relay_clock import CFG, SkewCorrectedMedian, SlewedOffset, sample_from_ping

pytestmark = [pytest.mark.requirement("B2BRELAY-02")]

OFFSET_US = 1_234_567.0
SKEW = 50e-6


def _peer(t_us: float) -> float:
    return OFFSET_US + t_us * (1.0 + SKEW)


def _true_offset(t_us: float) -> float:
    return _peer(t_us) - t_us


def _run(est: SkewCorrectedMedian, seconds: float, *, up_ms: callable, down_ms: callable, seed: int = 7) -> list[float]:
    """4 Hz pings for ``seconds``; returns |error| (us) of every estimate after the first minute."""
    rng = random.Random(seed)
    errors: list[float] = []
    t = 0.0
    while t < seconds * 1e6:
        up, down = up_ms(rng) * 1000.0, down_ms(rng) * 1000.0
        sample = sample_from_ping(t, _peer(t + up), t + up + down)
        est.add(sample)
        now = t + up + down
        got = est.estimate(now)
        if got is not None and t > 60e6:
            errors.append(abs(got.offset_us - _true_offset(now)))
        t += 250_000.0
    return errors


def _p95(values: list[float]) -> float:
    return sorted(values)[int(0.95 * (len(values) - 1))]


def _clean(rng: random.Random) -> float:
    return 0.2 + rng.random() * 0.05


def test_skew_tracking_keeps_a_clean_path_under_a_millisecond_and_the_control_bites(monkeypatch: pytest.MonkeyPatch) -> None:
    errors = _run(SkewCorrectedMedian(), 600, up_ms=_clean, down_ms=_clean)
    assert _p95(errors) < 100.0, f"if the clean-path p95 error is above 0.1 ms then broken: {_p95(errors):.0f} us"
    monkeypatch.setattr(relay_clock.CFG, "MIN_SKEW_BUCKETS", 10**9)  # control: no skew term
    control = _run(SkewCorrectedMedian(), 600, up_ms=_clean, down_ms=_clean)
    # Without the slope the 60 s median lags the 50 us/s ramp by about 30 s: about 1.5 ms.
    assert _p95(control) > 1000.0, (
        f"if dropping the skew term does not cost 1+ ms at 50 ppm then this test cannot bite: {_p95(control):.0f} us"
    )


def test_one_sided_queueing_is_filtered_by_round_trip() -> None:
    def queued(rng: random.Random) -> float:  # an upload: one direction queues up to 20 ms
        return 20.0 + rng.random() * 40.0 if rng.random() < 0.8 else 20.0

    def fixed(rng: random.Random) -> float:
        return 20.0

    est = SkewCorrectedMedian()
    errors = _run(est, 300, up_ms=queued, down_ms=fixed)
    assert _p95(errors) < 2000.0, f"if one-sided queueing biases the estimate over 2 ms then broken: {_p95(errors):.0f} us"
    # Control: the plain median of the same kind of samples (what the fast-quarter filter removes).
    rng = random.Random(7)
    plain = []
    for i in range(400):
        t = i * 250_000.0
        up, down = queued(rng) * 1000.0, fixed(rng) * 1000.0
        plain.append(sample_from_ping(t, _peer(t + up), t + up + down).offset_us - _true_offset(t + (up + down) / 2))
    assert abs(statistics.median(plain)) > 5000.0, "if a plain median is not biased here the queueing case is not exercised"


def test_no_estimate_until_enough_samples_and_skew_reports_not_ready() -> None:
    est = SkewCorrectedMedian()
    for i in range(CFG.MIN_SAMPLES - 1):
        est.add(sample_from_ping(i * 250e3, _peer(i * 250e3 + 100), i * 250e3 + 200))
    assert est.estimate(2e6) is None, "if an estimate comes before MIN_SAMPLES then broken"
    est.add(sample_from_ping(2e6, _peer(2e6 + 100), 2e6 + 200))
    got = est.estimate(2e6 + 200)
    assert got is not None and got.skew_ready is False and got.skew_ppm == 0.0, got


def test_a_ping_received_before_it_was_sent_is_refused() -> None:
    with pytest.raises(ValueError, match="before it was sent"):
        sample_from_ping(10.0, 5.0, 9.0)


def test_slew_steps_once_then_moves_at_a_bounded_rate_and_never_overshoots() -> None:
    slew = SlewedOffset()
    assert slew.update(1000.0, 0.0) == 1000.0 and slew.steps == 1, "if the first estimate does not step then broken"
    moved = slew.update(3000.0, 1e6)  # 2 ms away, 1 s later: at most SLEW_US_PER_S
    assert moved == pytest.approx(1000.0 + CFG.SLEW_US_PER_S), f"if a small disagreement jumps then broken: {moved}"
    for k in range(2, 20):
        moved = slew.update(3000.0, k * 1e6)
        assert moved <= 3000.0, f"if the slew overshoots the estimate then broken: {moved}"
    assert moved == 3000.0, "if the slew never arrives then broken"
    far = slew.update(3000.0 + CFG.STEP_US + 1, 21e6)
    assert far == 3000.0 + CFG.STEP_US + 1 and slew.steps == 2, "if a disagreement beyond STEP_US does not step then broken"
