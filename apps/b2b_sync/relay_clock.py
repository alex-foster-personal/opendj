"""B2BRELAY-02: the Link relay's clock estimator, skew-corrected median over min-RTT samples, slewed.

The relay (``link_relay``) maps the ON AIR laptop's Link timeline onto this laptop's Link host
clock, so it needs ``offset(t) = peer Link time - local Link time``. Two laptops' crystals run
apart by tens of ppm, so the offset is a line, not a constant.

The choice comes from spike S1 (``docs/b2b-link/INTERNET-COLLAB-RESEARCH.md`` section 5, netns +
netem, 180 s per profile, peer clock injected at +1.234567 s and 50 ppm). |error| p95 in ms:

==================  =========  =================  ===================  ===========================
profile             min_rtt    median_100 (Link)  min_rtt_regression   skew_corrected_median
==================  =========  =================  ===================  ===========================
clean               2.78       0.64               0.02                 0.60
regional (41 ms)    5.23       2.43               7.31                 6.46
intercontinental    11.76      1.27               3.77                 2.38
bursty              7.63       2.20               5.70                 2.20
upload paced        5.25       10.67              1.97                 1.37
==================  =========  =================  ===================  ===========================

``skew_corrected_median`` has the best worst case on every row a ping estimator can fix (the
asymmetric and unpaced-upload rows defeat all four, by half the asymmetry). The Link-style
median loses 10 ms to one-sided queueing, and the min-RTT pick we ship in the page ages with
skew. So this module is that estimator, ported from the spike with time windows instead of
sample counts (pings can be lost): the skew is the slope of a least-squares line through the
least-delayed sample of each 5 s bucket over up to 10 min; the offset is the median of the
skew-corrected offsets of the fastest quarter of the last 60 s.

:class:`SlewedOffset` then moves the applied offset toward the estimate at a bounded rate, so a
new estimate never jumps the follower's timeline (research risk 6); only the first estimate,
or a disagreement too large to be estimator noise, steps.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass


class CFG:
    #: The offset is the median over this many seconds of recent samples (the spike's 240 pings at 4 Hz).
    RECENT_S: float = 60.0
    #: The skew line is fitted over up to this much history (the spike's 2400 pings).
    SKEW_WINDOW_S: float = 600.0
    #: One least-delayed sample per bucket feeds the skew line (the spike's 20 pings at 4 Hz).
    BUCKET_S: float = 5.0
    #: Fewer bucket minima than this gives no slope yet (skew reads 0, and ``skew_ready`` false).
    MIN_SKEW_BUCKETS: int = 6
    #: The offset median uses the fastest fraction of recent samples by round trip.
    FAST_FRACTION: float = 0.25
    #: No estimate before this many samples (2 s of 4 Hz pings).
    MIN_SAMPLES: int = 8
    #: A fitted skew beyond this is not a crystal (+/-20 to 50 ppm typical); it is clamped and reported.
    MAX_SKEW_PPM: float = 500.0
    #: The applied offset moves toward the estimate at most this fast (us per second). 50 ppm of skew
    #: moves the true offset 50 us/s, so 500 us/s tracks any real crystal with a 10x margin.
    SLEW_US_PER_S: float = 500.0
    #: A disagreement above this is not estimator noise (S1 worst fixable p99 is 8 ms): step to it.
    STEP_US: float = 20_000.0


@dataclass(frozen=True)
class ClockSample:
    """One ping: local send/receive times bracket the peer's single stamp (the /link/clock/ping shape)."""

    #: Local Link time (us) halfway between send and receive.
    t_mid_us: float
    #: Peer Link time minus local Link time at ``t_mid_us`` (us).
    offset_us: float
    #: Round trip (us).
    rtt_us: float


def sample_from_ping(t_send_us: float, t_peer_us: float, t_recv_us: float) -> ClockSample:
    if not (math.isfinite(t_send_us) and math.isfinite(t_peer_us) and math.isfinite(t_recv_us)):
        raise ValueError(f"ping stamps must be finite, got {t_send_us}, {t_peer_us}, {t_recv_us}")
    if t_recv_us < t_send_us:
        raise ValueError(f"ping received ({t_recv_us}) before it was sent ({t_send_us}): not a monotonic clock")
    mid = (t_send_us + t_recv_us) / 2.0
    return ClockSample(t_mid_us=mid, offset_us=t_peer_us - mid, rtt_us=t_recv_us - t_send_us)


@dataclass(frozen=True)
class ClockEstimate:
    #: Peer Link time minus local Link time at the ``now_us`` it was asked for (us).
    offset_us: float
    #: d(offset)/dt in ppm: the peer clock runs this much faster than ours.
    skew_ppm: float
    #: Whether enough bucket minima exist for a slope (False: ``skew_ppm`` is 0 by construction).
    skew_ready: bool
    #: Whether the fitted slope was beyond ``CFG.MAX_SKEW_PPM`` and clamped.
    skew_clamped: bool
    #: Least and median round trip of the recent window (us).
    rtt_min_us: float
    rtt_p50_us: float
    #: Samples in the recent window, and those the median used.
    samples: int
    used: int


def _slope(points: list[ClockSample]) -> float:
    tbar = statistics.fmean(p.t_mid_us for p in points)
    obar = statistics.fmean(p.offset_us for p in points)
    var = sum((p.t_mid_us - tbar) ** 2 for p in points)
    if var == 0:
        raise ValueError("bucket minima share one timestamp; no slope can be fitted")
    return sum((p.t_mid_us - tbar) * (p.offset_us - obar) for p in points) / var


class SkewCorrectedMedian:
    """Rolling samples in, offset and skew out. Pure: no clock of its own (``now_us`` is passed)."""

    def __init__(self) -> None:
        self._samples: deque[ClockSample] = deque()

    def __len__(self) -> int:
        return len(self._samples)

    def add(self, sample: ClockSample) -> None:
        if self._samples and sample.t_mid_us < self._samples[-1].t_mid_us:
            raise ValueError("clock samples must arrive in local time order")
        self._samples.append(sample)
        horizon = sample.t_mid_us - CFG.SKEW_WINDOW_S * 1e6
        while self._samples and self._samples[0].t_mid_us < horizon:
            self._samples.popleft()

    def _bucket_minima(self) -> list[ClockSample]:
        buckets: dict[int, ClockSample] = {}
        width = CFG.BUCKET_S * 1e6
        for sample in self._samples:
            key = int(sample.t_mid_us // width)
            best = buckets.get(key)
            if best is None or sample.rtt_us < best.rtt_us:
                buckets[key] = sample
        return [buckets[key] for key in sorted(buckets)]

    def estimate(self, now_us: float) -> ClockEstimate | None:
        """None until ``CFG.MIN_SAMPLES`` recent samples exist (the caller says why it waits)."""
        recent = [s for s in self._samples if s.t_mid_us >= now_us - CFG.RECENT_S * 1e6]
        if len(recent) < CFG.MIN_SAMPLES:
            return None
        minima = self._bucket_minima()
        skew_ready = len(minima) >= CFG.MIN_SKEW_BUCKETS
        slope = _slope(minima) if skew_ready else 0.0
        limit = CFG.MAX_SKEW_PPM * 1e-6
        clamped = abs(slope) > limit
        if clamped:
            slope = math.copysign(limit, slope)
        rtts = sorted(s.rtt_us for s in recent)
        cutoff = rtts[min(len(rtts) - 1, int(len(rtts) * CFG.FAST_FRACTION))]  # the spike's sorted(...)[n // 4]
        fast = [s.offset_us + slope * (now_us - s.t_mid_us) for s in recent if s.rtt_us <= cutoff]
        return ClockEstimate(
            offset_us=statistics.median(fast),
            skew_ppm=slope * 1e6,
            skew_ready=skew_ready,
            skew_clamped=clamped,
            rtt_min_us=rtts[0],
            rtt_p50_us=statistics.median(rtts),
            samples=len(recent),
            used=len(fast),
        )


class SlewedOffset:
    """The offset the relay applies: steps once, then slews toward each new estimate."""

    def __init__(self) -> None:
        self.applied_us: float | None = None
        self._at_us: float | None = None
        self.steps = 0

    def update(self, estimate_us: float, now_us: float) -> float:
        if self.applied_us is None or self._at_us is None or abs(estimate_us - self.applied_us) > CFG.STEP_US:
            self.applied_us, self._at_us = estimate_us, now_us
            self.steps += 1
            return estimate_us
        budget = CFG.SLEW_US_PER_S * max(0.0, now_us - self._at_us) / 1e6
        delta = estimate_us - self.applied_us
        self.applied_us += max(-budget, min(budget, delta))
        self._at_us = now_us
        return self.applied_us
