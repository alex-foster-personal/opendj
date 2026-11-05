"""B2B continuity SLOs (B2BSLO-01..04): the sample wire format, the thresholds, and the ONE scorer.

The page records what it measures while a B2B Link session runs (``link-continuity.svelte.ts``)
and posts it to its own engine (``POST /api/v1/link/continuity/samples``); agents and the soak
harness read it back through ``GET /api/v1/link/continuity`` (MCP ``b2b_continuity``, CLI
``opendj link_continuity``). The soak (``tests/b2b_e2e/test_continuity_soak.py``) and the route's
summary both score with the functions below, so every round in ``(private path)``
is scored by the same code against the same thresholds.

Sample kinds (each stamped by the page with its Link host time estimate ``t_link_us`` and the
session beat at that moment). Link shares tempo and PHASE across peers, but not the absolute beat
count (two peers' beats can differ by whole bars, ``link-session.svelte.ts``), and two machines
share no host clock either. So tempo propagation, the one cross-laptop delta, is scored only when
both pages read ONE host clock (the single-host rig), where it is computed on session beats and
cross-checked proposal by proposal against the host clock; any disagreement, or two machines,
reads ``unknown``, never a number:

- ``phase``          one phase-lock measurement of the master deck vs the session (4 Hz).
- ``tempo_proposed`` the ON AIR page proposed a new session tempo to Link.
- ``tempo_seen``     a page's Link poll first saw a new session tempo.
- ``tempo_applied``  a FOLLOWING page set its master deck to a new session tempo.
- ``drop``           the B2B drop policy changed phase (normal/held/solo/restored).
- ``gap``            an audible-gap perf event (xrun, silent-while-playing, audio-output-dead).

Run ``python -m apps.b2b_sync.link_continuity`` to print the SLO table.
"""

from __future__ import annotations

import bisect
import json
import math
import statistics
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Annotated, Final, Literal

from pydantic import BaseModel, Field

from apps.shared.link_constants import LINK

# ----------------------------------------------------------------------------- SLO thresholds

#: B2BSLO-01: audible gaps per B2B hour, from the perf kinds below. Any gap fails.
GAPS_PER_HOUR_MAX: Final[float] = 0.0
#: The perf kinds that mean "the DJ may have heard a gap". ``audio-output-dead-persistent`` is the
#: same verdict held longer, so it counts too.
GAP_PERF_KINDS: Final[tuple[str, ...]] = LINK.gap_perf_kinds  # LINKCONST-01: apps/shared/link_constants.json
#: B2BSLO-02: |phase error| p95 while FOLLOWING, ms. CONTRACT.md's take-over threshold (a deck
#: more than 5 ms out is forced). TO CALIBRATE WITH MAINTAINER BY EAR before it gates anything.
PHASE_ERROR_P95_MAX_MS: Final[float] = 5.0
#: B2BSLO-03: ON AIR proposes a tempo -> the FOLLOWING page applies it to its master deck, p95, ms.
TEMPO_PROPAGATION_P95_MAX_MS: Final[float] = 250.0
#: B2BSLO-04: a connection drop heals -> the follower's |phase error| is back within the lock
#: threshold, ms, for every drop, with zero gaps from the drop's start to that moment.
RESYNC_MAX_MS: Final[float] = 2000.0
#: The lock threshold the resync waits for (LINKCONST-01: the page's LOCKED_MS, one JSON source).
LOCKED_MS: Final[float] = LINK.locked_ms
#: B2BSLO-04: share of drops up to this long that the session holds through (no gap, no Play
#: solo, no leave, tempo kept, phase back). Applied to drops of AT MOST this length, which is
#: the stricter reading of "drops < 30 s": the 30 s partition profile is held to it too.
HELD_THROUGH_MAX_DROP_S: Final[float] = 30.0
HELD_THROUGH_MIN_RATE: Final[float] = 1.0
#: Two tempos within this are the same proposal (link-session LINK_TEMPO_TOLERANCE_BPM order).
TEMPO_MATCH_BPM: Final[float] = 0.05
#: B2BSLO-03 instrument check: one proposal's propagation on session beats and on the shared host
#: clock must agree within this. Rounds 0 and 1 agreed within a few ms; a whole-bar beat offset
#: between the peers is about 2 s at DJ tempos, so it cannot hide under this tolerance.
TEMPO_CROSSCHECK_MS: Final[float] = 50.0
#: B2BSLO-03 on one host clock: Link shares phase only within the quantum, so the two pages' beat
#: COUNTS may differ by whole bars for a whole session (soak round 2: exactly 4 beats, every proposal
#: read inf). The offset is measured from phase samples the two pages took within
#: BAR_OFFSET_PAIR_MAX_US of each other; at least BAR_OFFSET_MIN_PAIRS pairs must ALL agree on one
#: whole number of bars within BAR_OFFSET_TOLERANCE_BEATS, or nothing is aligned and the
#: host-clock cross-check decides.
LINK_QUANTUM_BEATS: Final[float] = 4.0
BAR_OFFSET_PAIR_MAX_US: Final[int] = 150_000
BAR_OFFSET_MIN_PAIRS: Final[int] = 10
BAR_OFFSET_TOLERANCE_BEATS: Final[float] = 0.05

SLO_NAMES: Final[tuple[str, ...]] = (
    "gaps_per_hour",
    "phase_error_p95_ms",
    "tempo_propagation_p95_ms",
    "resync_max_ms",
    "held_through_rate",
)

# ----------------------------------------------------------------------------- wire format

Role = Literal["on_air", "following"]
DropPhase = Literal["normal", "held", "solo", "restored"]


class _Stamped(BaseModel):
    #: The page's estimate of Link host time (us) when it measured this, or null before its
    #: first clock ping (only ``gap`` may be unstamped).
    t_link_us: int | None
    #: The session beat at ``t_link_us`` (phase is shared by peers, the whole-bar count is not), or
    #: null with no timeline yet.
    session_beat: float | None


class PhaseSample(_Stamped):
    kind: Literal["phase"]
    t_link_us: int
    role: Role
    lock_state: str
    deck: int
    bpm: float
    #: Signed beat-phase error of the deck vs the held session phase, ms.
    error_ms: float


class TempoSample(_Stamped):
    kind: Literal["tempo_proposed", "tempo_seen", "tempo_applied"]
    t_link_us: int
    bpm: float
    deck: int | None


class DropSample(_Stamped):
    kind: Literal["drop"]
    t_link_us: int
    phase_from: DropPhase
    phase_to: DropPhase
    drop_kind: str | None
    bpm: float | None


class GapSample(_Stamped):
    kind: Literal["gap"]
    perf_kind: str
    severity: str
    message: str


ContinuitySample = Annotated[PhaseSample | TempoSample | DropSample | GapSample, Field(discriminator="kind")]


class ContinuityBatchIn(BaseModel):
    #: The posting page's id (one engine may see a reload's old and new page).
    page_id: str = Field(min_length=1, max_length=128)
    #: Unique per batch the page takes, and REUSED when it retries that batch, so a retry whose
    #: first POST reached the engine (only the answer was lost) is recognized and not stored twice.
    batch_id: str = Field(min_length=1, max_length=128)
    samples: list[ContinuitySample] = Field(max_length=2000)
    #: Samples the page's bounded queue dropped (oldest first) since its previous ACCEPTED batch,
    #: reported once each. Non-zero means the window has holes the scorer cannot see.
    dropped: int = Field(ge=0)


# ----------------------------------------------------------------------------- scorer

Verdict = Literal["pass", "fail", "unknown", "n/a"]


@dataclass(frozen=True)
class Partition:
    """One injected drop, in the FOLLOWER engine's Link host time (us).

    ``start``/``heal`` bracket the cut as the harness observed it (read just before the cut and
    just after the heal, so the window is a little longer than the cut); ``nominal_s`` is the cut
    the harness injected, which is what the held-through scope ("drops up to 30 s") is about.
    Round 0 scoped by the observed window, so its 30 s cuts (observed 30.3 to 30.5 s) fell out.
    """

    start_link_us: int
    heal_link_us: int
    nominal_s: float

    @property
    def seconds(self) -> float:
        return (self.heal_link_us - self.start_link_us) / 1e6


@dataclass(frozen=True)
class Percentiles:
    n: int
    p50: float | None
    p95: float | None
    p99: float | None
    max: float | None


@dataclass(frozen=True)
class SloResult:
    name: str
    threshold: float
    measured: float | None
    verdict: Verdict
    detail: str


@dataclass
class ProfileScore:
    profile: str
    soak_s: float
    slos: list[SloResult]
    phase_error_ms: Percentiles
    tempo_propagation_ms: Percentiles
    resync_ms: list[float | None]
    counts: dict[str, int] = field(default_factory=dict)

    def slo(self, name: str) -> SloResult:
        return next(result for result in self.slos if result.name == name)

    def to_json(self) -> dict[str, object]:
        return asdict(self)


def percentiles(values: Sequence[float]) -> Percentiles:
    """Nearest-rank p50/p95/p99 (inf allowed: an unanswered proposal is an infinite latency)."""
    if not values:
        return Percentiles(0, None, None, None, None)
    ordered = sorted(values)

    def rank(p: float) -> float:
        return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]

    return Percentiles(len(ordered), rank(50), rank(95), rank(99), ordered[-1])


def _inside(t_us: int | None, windows: Iterable[tuple[int, int]]) -> bool:
    return t_us is not None and any(lo <= t_us <= hi for lo, hi in windows)


def _drop_windows(partitions: Sequence[Partition]) -> list[tuple[int, int]]:
    """A drop's own window: from its start to heal + the resync budget (scored by B2BSLO-04)."""
    return [(p.start_link_us, p.heal_link_us + int(RESYNC_MAX_MS * 1000)) for p in partitions]


def following_phase_errors(follower: Sequence[ContinuitySample], partitions: Sequence[Partition]) -> list[float]:
    """|error| of every FOLLOWING phase sample outside the drop windows, ms."""
    windows = _drop_windows(partitions)
    return [
        abs(s.error_ms)
        for s in follower
        if isinstance(s, PhaseSample) and s.role == "following" and not _inside(s.t_link_us, windows)
    ]


def session_bar_offset_beats(leader: Sequence[ContinuitySample], follower: Sequence[ContinuitySample]) -> float | None:
    """The leader's session beat count minus the follower's, a whole number of bars, measured on ONE
    host clock from phase samples the two pages took at nearly the same moment; None when there are
    too few pairs or they do not all agree on one whole-bar offset (never a guess)."""
    lead = sorted(
        (s for s in leader if isinstance(s, PhaseSample) and s.session_beat is not None), key=lambda s: s.t_link_us
    )
    times = [s.t_link_us for s in lead]
    diffs: list[float] = []
    for f in follower:
        if not isinstance(f, PhaseSample) or f.session_beat is None or not lead:
            continue
        i = bisect.bisect_left(times, f.t_link_us)
        near = min(lead[max(0, i - 1) : i + 1], key=lambda s: abs(s.t_link_us - f.t_link_us))
        if abs(near.t_link_us - f.t_link_us) > BAR_OFFSET_PAIR_MAX_US or abs(near.bpm - f.bpm) > TEMPO_MATCH_BPM:
            continue
        assert near.session_beat is not None
        diffs.append(near.session_beat - f.session_beat - (near.t_link_us - f.t_link_us) * f.bpm / 60e6)
    if len(diffs) < BAR_OFFSET_MIN_PAIRS:
        return None
    bars = round(statistics.median(diffs) / LINK_QUANTUM_BEATS) * LINK_QUANTUM_BEATS
    if any(abs(d - bars) > BAR_OFFSET_TOLERANCE_BEATS for d in diffs):
        return None
    return float(bars)


def _applied_answer(proposal: TempoSample, until_beat: float | None, applied: Sequence[TempoSample]) -> float:
    """ms from one proposal to the follower applying its bpm before ``until_beat``; inf if never."""
    start = proposal.session_beat
    if start is None:
        return math.inf
    for answer in applied:
        beat = answer.session_beat
        in_window = beat is not None and beat >= start and (until_beat is None or beat < until_beat)
        if in_window and abs(answer.bpm - proposal.bpm) <= TEMPO_MATCH_BPM:
            assert beat is not None
            return (beat - start) * 60_000.0 / proposal.bpm
    return math.inf


def tempo_propagations(
    leader: Sequence[ContinuitySample], follower: Sequence[ContinuitySample], follower_bar_offset_beats: float = 0.0
) -> list[float]:
    """For each ON AIR proposal, ms (in session beats at the new tempo) until the follower applied it.

    A proposal the follower never applied before the next proposal is ``inf``: it counts against
    the p95 instead of vanishing from the denominator. ``follower_bar_offset_beats`` (a measured
    ``session_bar_offset_beats``) puts the follower's beats on the leader's count first.
    """
    proposals = [s for s in leader if isinstance(s, TempoSample) and s.kind == "tempo_proposed"]
    applied = [
        s.model_copy(update={"session_beat": s.session_beat + follower_bar_offset_beats})
        if s.session_beat is not None
        else s
        for s in follower
        if isinstance(s, TempoSample) and s.kind == "tempo_applied"
    ]
    untils: list[float | None] = [p.session_beat for p in proposals[1:]] + ([None] if proposals else [])
    return [_applied_answer(p, until, applied) for p, until in zip(proposals, untils, strict=True)]


def host_clock_propagations_ms(
    leader: Sequence[ContinuitySample], follower: Sequence[ContinuitySample]
) -> list[float | None]:
    """The same pairing as ``tempo_propagations`` on Link host time (valid only when both pages read
    ONE host clock): each proposal's first matching application before the NEXT proposal, ms, or
    None if it never came."""
    proposals = [s for s in leader if isinstance(s, TempoSample) and s.kind == "tempo_proposed"]
    applied = [s for s in follower if isinstance(s, TempoSample) and s.kind == "tempo_applied"]
    untils: list[int | None] = [p.t_link_us for p in proposals[1:]] + ([None] if proposals else [])

    def answer_ms(proposal: TempoSample, until_us: int | None) -> float | None:
        match = next(
            (
                a
                for a in applied
                if a.t_link_us >= proposal.t_link_us
                and (until_us is None or a.t_link_us < until_us)
                and abs(a.bpm - proposal.bpm) <= TEMPO_MATCH_BPM
            ),
            None,
        )
        return None if match is None else (match.t_link_us - proposal.t_link_us) / 1000.0

    return [answer_ms(p, until) for p, until in zip(proposals, untils, strict=True)]


def tempo_crosscheck_disagreements(
    leader: Sequence[ContinuitySample], follower: Sequence[ContinuitySample], follower_bar_offset_beats: float = 0.0
) -> list[str]:
    """Proposals whose beat-time and host-clock propagation disagree (one clock assumed).

    A proposal the leader could not place on its timeline (no session beat) is ``inf`` by rule and
    is not compared. Anything else must agree: both unanswered, or both answered within
    TEMPO_CROSSCHECK_MS. A whole-bar beat-count offset between the peers shows up here.
    """
    proposals = [s for s in leader if isinstance(s, TempoSample) and s.kind == "tempo_proposed"]
    beats = tempo_propagations(leader, follower, follower_bar_offset_beats)
    hosts = host_clock_propagations_ms(leader, follower)
    out: list[str] = []
    for proposal, beat_ms, host_ms in zip(proposals, beats, hosts, strict=True):
        if proposal.session_beat is None:
            continue
        agree = (math.isinf(beat_ms) and host_ms is None) or (
            host_ms is not None and math.isfinite(beat_ms) and abs(beat_ms - host_ms) <= TEMPO_CROSSCHECK_MS
        )
        if not agree:
            out.append(
                f"{proposal.bpm:.2f} BPM at {proposal.t_link_us}: beats {beat_ms:.1f} ms vs host clock {host_ms} ms"
            )
    return out


def resync_after(follower: Sequence[ContinuitySample], partition: Partition) -> float | None:
    """ms from heal to the first FOLLOWING phase sample within LOCKED_MS, or None if it never came."""
    after = sorted(
        (
            s
            for s in follower
            if isinstance(s, PhaseSample) and s.role == "following" and s.t_link_us >= partition.heal_link_us
        ),
        key=lambda s: s.t_link_us,
    )
    first = next((s for s in after if abs(s.error_ms) <= LOCKED_MS), None)
    return None if first is None else (first.t_link_us - partition.heal_link_us) / 1000.0


#: The only severity that is an audible gap (B2BSLO-01); a ``warn`` row is the rate the live
#: invariants accept, so it never counts (the page filters the same way, ``isAudibleGap``).
GAP_SEVERITY: Final[str] = "error"


def gaps(samples: Sequence[ContinuitySample]) -> list[GapSample]:
    return [
        s for s in samples if isinstance(s, GapSample) and s.perf_kind in GAP_PERF_KINDS and s.severity == GAP_SEVERITY
    ]


def _gaps_in(samples: Sequence[GapSample], lo_us: int, hi_us: int) -> int:
    """Gaps inside [lo, hi]; an unstamped gap cannot be placed, so it counts against every window."""
    return sum(1 for g in samples if g.t_link_us is None or lo_us <= g.t_link_us <= hi_us)


def _drop_window_end(partition: Partition) -> int:
    return partition.heal_link_us + int(RESYNC_MAX_MS * 1000)


def held_through(
    follower: Sequence[ContinuitySample], all_gaps: Sequence[GapSample], partition: Partition, resync_ms: float | None
) -> bool:
    """The session held through this drop: no gap, never solo, tempo kept, phase back in budget."""
    hi = _drop_window_end(partition)
    drops = [s for s in follower if isinstance(s, DropSample) and partition.start_link_us <= s.t_link_us <= hi]
    went_solo = any(d.phase_to == "solo" for d in drops)
    return (
        _gaps_in(all_gaps, partition.start_link_us, hi) == 0
        and not went_solo
        and resync_ms is not None
        and resync_ms <= RESYNC_MAX_MS
    )


def _verdict(measured: float | None, threshold: float, *, at_most: bool = True) -> Verdict:
    if measured is None:
        return "unknown"
    return "pass" if (measured <= threshold if at_most else measured >= threshold) else "fail"


# ----------------------------------------------------------------------------- one helper per SLO


def _gap_slo(all_gaps: Sequence[GapSample], phase_samples: int, gap_instrument_live: bool, soak_s: float) -> SloResult:
    measuring = phase_samples > 0 and gap_instrument_live
    # A gap is a fail whether or not the instrument is otherwise trusted: it was heard.
    verdict: Verdict = "fail" if all_gaps else ("pass" if measuring else "unknown")
    rate = len(all_gaps) * 3600.0 / soak_s
    detail = f"{len(all_gaps)} gaps in {soak_s / 60:.1f} min"
    if not phase_samples:
        detail += "; no phase samples, so the page was not measuring"
    if not gap_instrument_live:
        detail += "; the xrun sentinel counted no audio callbacks, so zero is unmeasured"
    return SloResult("gaps_per_hour", GAPS_PER_HOUR_MAX, rate if (measuring or all_gaps) else None, verdict, detail)


def _phase_slo(phase: Percentiles) -> SloResult:
    return SloResult(
        "phase_error_p95_ms",
        PHASE_ERROR_P95_MAX_MS,
        phase.p95,
        _verdict(phase.p95, PHASE_ERROR_P95_MAX_MS),
        f"n={phase.n} FOLLOWING samples outside drop windows",
    )


def _tempo_slo(
    tempo: Percentiles,
    values: Sequence[float],
    *,
    shared_host_clock: bool,
    disagreements: Sequence[str],
    bar_offset_beats: float = 0.0,
) -> SloResult:
    if not shared_host_clock:
        return SloResult(
            "tempo_propagation_p95_ms",
            TEMPO_PROPAGATION_P95_MAX_MS,
            None,
            "unknown",
            "two host clocks: Link shares phase, not the beat count, so a cross-machine latency needs "
            "explicit proposal correlation, which this scorer does not have",
        )
    if disagreements:
        return SloResult(
            "tempo_propagation_p95_ms",
            TEMPO_PROPAGATION_P95_MAX_MS,
            None,
            "unknown",
            f"beat-time and host-clock propagation disagree on {len(disagreements)} of {tempo.n} proposals "
            f"(peers' beat counts not aligned?): {list(disagreements)[:3]}",
        )
    return SloResult(
        "tempo_propagation_p95_ms",
        TEMPO_PROPAGATION_P95_MAX_MS,
        tempo.p95,
        _verdict(tempo.p95, TEMPO_PROPAGATION_P95_MAX_MS),
        f"n={tempo.n} proposals, {sum(1 for v in values if math.isinf(v))} never applied"
        + (
            f"; the follower's beat count aligned by {bar_offset_beats:+g} beats "
            f"({abs(bar_offset_beats) / LINK_QUANTUM_BEATS:g} bar), measured on the shared host clock"
            if bar_offset_beats
            else ""
        ),
    )


def _resync_slo(
    partitions: Sequence[Partition], resync: Sequence[float | None], all_gaps: Sequence[GapSample]
) -> SloResult:
    if not partitions:
        return SloResult("resync_max_ms", RESYNC_MAX_MS, None, "n/a", "no drops injected")
    worst = math.inf if any(r is None for r in resync) else max(r for r in resync if r is not None)
    window_gaps = sum(_gaps_in(all_gaps, p.start_link_us, _drop_window_end(p)) for p in partitions)
    verdict: Verdict = "fail" if window_gaps else _verdict(worst, RESYNC_MAX_MS)
    detail = f"{len(partitions)} drops, {window_gaps} gaps inside drop windows, per drop ms: {list(resync)}"
    return SloResult("resync_max_ms", RESYNC_MAX_MS, worst, verdict, detail)


def _held_slo(
    follower: Sequence[ContinuitySample],
    all_gaps: Sequence[GapSample],
    partitions: Sequence[Partition],
    resync: Sequence[float | None],
) -> SloResult:
    held = [
        held_through(follower, all_gaps, p, r)
        for p, r in zip(partitions, resync, strict=True)
        if p.nominal_s <= HELD_THROUGH_MAX_DROP_S
    ]
    if not held:
        return SloResult("held_through_rate", HELD_THROUGH_MIN_RATE, None, "n/a", "no drops of at most 30 s injected")
    rate = sum(held) / len(held)
    verdict = _verdict(rate, HELD_THROUGH_MIN_RATE, at_most=False)
    return SloResult(
        "held_through_rate", HELD_THROUGH_MIN_RATE, rate, verdict, f"{sum(held)}/{len(held)} drops held through"
    )


def _drop_slos_unknown_across_clocks() -> list[SloResult]:
    """Partition bounds are in the FOLLOWER engine's clock; with two host clocks the leader's gaps
    cannot be placed inside them, so neither drop SLO can be scored (never a pass or a fail)."""
    why = "two host clocks: partition bounds are in the follower's clock, the leader's gaps cannot be placed"
    return [
        SloResult("resync_max_ms", RESYNC_MAX_MS, None, "unknown", why),
        SloResult("held_through_rate", HELD_THROUGH_MIN_RATE, None, "unknown", why),
    ]


def score_profile(
    profile: str,
    leader: Sequence[ContinuitySample],
    follower: Sequence[ContinuitySample],
    partitions: Sequence[Partition],
    soak_s: float,
    *,
    gap_instrument_live: bool,
    shared_host_clock: bool,
) -> ProfileScore:
    """Score one soak profile. ``unknown`` when a SLO had nothing to measure; never a pass.

    ``gap_instrument_live``: the pages' xrun sentinel counted audio callbacks during the window
    (read from the ui-mirror). Without it zero gaps is an unmeasured zero, so the gap SLO reads
    ``unknown`` rather than ``pass``.

    ``shared_host_clock``: both pages read ONE Link host clock (the single-host rig). Only then are
    tempo propagation (cross-checked on that clock) and, with partitions, the two drop SLOs scored;
    otherwise they read ``unknown``.
    """
    if soak_s <= 0:
        raise ValueError(f"soak_s must be positive, got {soak_s}")
    all_gaps = gaps([*leader, *follower])
    phase = percentiles(following_phase_errors(follower, partitions))
    bar_offset = (session_bar_offset_beats(leader, follower) if shared_host_clock else None) or 0.0
    tempo_values = tempo_propagations(leader, follower, bar_offset)
    tempo = percentiles(tempo_values)
    resync = [resync_after(follower, p) for p in partitions]
    phase_samples = sum(1 for s in follower if isinstance(s, PhaseSample))
    slos = [
        _gap_slo(all_gaps, phase_samples, gap_instrument_live, soak_s),
        _phase_slo(phase),
        _tempo_slo(
            tempo,
            tempo_values,
            shared_host_clock=shared_host_clock,
            disagreements=tempo_crosscheck_disagreements(leader, follower, bar_offset) if shared_host_clock else [],
            bar_offset_beats=bar_offset,
        ),
        *(
            [_resync_slo(partitions, resync, all_gaps), _held_slo(follower, all_gaps, partitions, resync)]
            if shared_host_clock or not partitions
            else _drop_slos_unknown_across_clocks()
        ),
    ]
    counts = {
        kind: sum(1 for s in [*leader, *follower] if s.kind == kind)
        for kind in ("phase", "tempo_proposed", "tempo_seen", "tempo_applied", "drop", "gap")
    }
    return ProfileScore(profile, soak_s, slos, phase, tempo, resync, counts)


def slo_table() -> list[dict[str, object]]:
    return [
        {"slo": "gaps_per_hour", "max": GAPS_PER_HOUR_MAX, "kinds": list(GAP_PERF_KINDS)},
        {"slo": "phase_error_p95_ms", "max": PHASE_ERROR_P95_MAX_MS, "note": "to calibrate with the maintainer by ear"},
        {"slo": "tempo_propagation_p95_ms", "max": TEMPO_PROPAGATION_P95_MAX_MS},
        {"slo": "resync_max_ms", "max": RESYNC_MAX_MS, "gaps_in_window_max": 0},
        {"slo": "held_through_rate", "min": HELD_THROUGH_MIN_RATE, "drops_up_to_s": HELD_THROUGH_MAX_DROP_S},
    ]


if __name__ == "__main__":
    json.dump(slo_table(), sys.stdout, indent=1)
    sys.stdout.write("\n")
