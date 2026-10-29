"""The macro measure M, layers 0 and 1, from a rendered transition (TRYER-01).

Input: the mix and each deck's own output (``odj-audio render --decks-out``:
after the channel strip and crossfader, before the master gain) plus a
:class:`RenderContext` saying what the plan meant (where the overlap is, where
the outgoing deck's downbeats fall, which step changes and seams it makes, the
tempo ratios it played). Output: a :class:`~apps.transition_score.contract.ScoreResult`.

Layer 0 (vetoes; any one sets M = 0 and names itself), file 15 section 2:
beat offset p95 > 20 ms, bar lag != 0, true peak > -1 dBTP made by the
transition, dropout (< -40 LUFS for > 0.5 s), stretch > 8 %, key shift > 2
semitones, parity (live capture vs render > 5 ms or 0.5 LU). Round 14 adds a
loudness-loss veto (the mix's short-term loudness more than 3 LU under what
the outgoing track would be playing on its own at that moment, for more than 2
bars of the overlap; mimicked from the constant-power crossfader's reason to
exist; skipped only for a plan that explicitly asks for the drop: intent
wind-down or shape lull-then-drop) that is measured on every render but OFF
(:data:`LOUDNESS_LOSS_VETO_ON`); loudness continuity's doubled weight carries the change.

Layer 1 (execution, hygiene mimicked): each term is squashed to 0..1 by
:func:`squash`, 1.0 at zero error and 0.5 at the term's KNEE, which is the
reference value cited beside it. M is the weighted geometric mean of the
measured layer-1 factors. The weights are PRIORS (file 15: Mosaikbox's rhythm
0.3 and harmony 0.2, PR #3873's v0 loudness 0.2) pending the Bradley-Terry fit
on the maintainer's execution preferences; nothing here is calibrated yet.

Never a verdict from a failed measurement: a factor that cannot be measured
goes to ``unknown`` with its reason and out of the composite, and with fewer
than :data:`~apps.transition_score.contract.MIN_CORE_MEASURED` core factors
measured there is no composite at all.

Beat offset is NOT folded modulo one beat on its own: the per-beat offset is
the sub-beat residual, and the whole-beat lag is measured separately from the
bar-folded accent profile (``downbeat_phase``) and vetoes on any non-zero
value, so a one-beat slip can never read as perfect alignment (spec 10 trap 4,
the v0 scorer's blind spot).
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from apps.autodj.propose import INTENTS, WIND_DOWN
from apps.transition_score import signals
from apps.transition_score.contract import (
    CORE_FACTORS,
    MIN_CORE_MEASURED,
    SCORER_VERSION,
    Measured,
    ScoreResult,
    Veto,
)

# ----- layer 0 thresholds (file 15 section 2; spec 10 section 3) -------------
BEAT_OFFSET_P95_VETO_MS = 20.0
TRUE_PEAK_VETO_DBTP = -1.0
#: The transition must add this much over the louder single deck's true
#: peak in the overlap (round 15; was the solo spans) before the true-peak veto fires: a master that itself peaks above
#: -1 dBTP is the track's mastering, not a fault of the transition.
#: 0.1 dB is the 4x-oversampled true-peak measurement tolerance (the loudness
#: promotion gate's), so a smaller rise is not attributable to the transition.
TRUE_PEAK_EXCESS_DB = 0.1
DROPOUT_LUFS = -40.0
DROPOUT_VETO_S = 0.5
STRETCH_VETO_PCT = 8.0
#: Mixxx's own key-shift bound (file 14).
KEY_SHIFT_VETO_ST = 2.0
PARITY_VETO_MS = 5.0
PARITY_VETO_LU = 0.5
#: Round 14 (the maintainer, oltf-r1 Thu 29 Oct 2026: "keep the vibes high and energy high, except for those few times a set
#: where you don't"). Hygiene, so MIMICKED, not calibrated: the mix held more than 3 LU under what the outgoing track
#: would be playing on its own is the hole in the middle of a linear crossfade (each deck at -6 dB, uncorrelated decks
#: summing to -3 dB), the defect the constant-power crossfader curve exists to remove (Mixxx's default, as in every
#: mainstream DJ mixer) ...
LOUDNESS_LOSS_VETO_LU = 3.0
#: ... held for longer than this many outgoing bars: sustained, i.e. longer than one EBU Tech 3341 short-term window
#: (3 s; 2 bars is 3.75 s at 128 BPM), not a single low reading. This is the task's stated length, also not calibrated.
LOUDNESS_LOSS_VETO_BARS = 2.0
#: The veto is measured and reported (``loudness_loss_bars``, ``loudness_loss_depth_lu``, ``loudness_loss_would_veto``)
#: but does not set M to 0: loudness continuity at weight 0.4 carries the change, and the proposer's level fixes
#: (#7809, #7818) remove the main cause of the dip; this veto is for the sustained, unambiguous hole once it is on.
LOUDNESS_LOSS_VETO_ON = False
#: The plan shape that asks for a lull: the outgoing window in a breakdown while a little of the incoming comes in,
#: its drop landing on the handover (the maintainer, Thu 29 Oct 2026: "a legit transition type"; the proposer writes it as
#: ``plan["shape"]``, #7818 D13). Exempt from the loudness-loss veto like a wind-down intent.
LULL_THEN_DROP = "lull_then_drop"
#: A mix back within this of the outgoing's own start level within one bar of the incoming bass hit has resolved its
#: lull (reported, not scored). Mimicked: EBU R128's +/-1 LU tolerance on a programme's loudness target.
RECOVERY_TOLERANCE_LU = 1.0

# ----- layer 1 knees, each a mimicked reference (CLAUDE.md: hygiene is mimicked) -----
#: Spec 04's landing tolerance for a beatmatched downbeat.
ALIGNMENT_KNEE_MS = 10.0
#: PR #3873's v0 loudness free band (straight line from A-solo to B-solo).
LOUDNESS_KNEE_LU = 1.5
#: Round 15: one beat with BOTH decks' lows near full (two basslines at once) is the free allowance.
BASS_BOTH_FULL_KNEE_BEATS = 1.0
#: Round 15, derived from the maintainer's hand rule (Thu 29 Oct 2026: incoming LO parked a third of a half turn down, about
#: -8.7 dB, brought to flat while the outgoing LO goes to where the incoming just was): the summed low band then
#: stays within about 1.5 dB of a steady low end (start 1.135 in power, midpoint 0.74). That excursion is what his
#: standard handoff does, so it must score at least the clean floor 0.8: the knee is twice it. He can move either.
LOW_RULE_EXCURSION_DB = 1.5
LOW_CONTINUITY_KNEE_DB = 2.0 * LOW_RULE_EXCURSION_DB
#: Round 15: the low end handed to the incoming this many bars before its own bass exists is the free allowance (the maintainer:
#: "you're taking away the bass, the vibe, before the new one is there").
EARLY_HANDOVER_KNEE_BARS = 1.0
#: low_hole (reported, not scored, until r5 reads clean): the mix this far under the dominant deck's own source low.
LOW_HOLE_DB = 3.0
#: Spec 10 / Mosaikbox: half a second of two vocals is the free allowance.
VOCAL_OVERLAP_KNEE_S = 0.5
#: Spec 04's landing tolerance again: a step change is a landing.
CUT_KNEE_MS = 10.0
#: Mixxx crossfades every loop seam and cut (file 13): the reference has none.
SEAM_CLICKS_KNEE = 0.5

#: PRIOR weights, pending the Bradley-Terry fit (file 15 section 2).
PRIOR_WEIGHTS: dict[str, float] = {
    "alignment": 0.3,  # Mosaikbox rhythm
    "downbeat_phase": 0.3,  # Mosaikbox rhythm
    "cut_to_downbeat": 0.3,  # Mosaikbox rhythm
    "vocal_overlap": 0.2,  # Mosaikbox harmony (vocal rules)
    "loudness_continuity": 0.4,  # PR #3873 v0 at 0.2; doubled in round 14 (the maintainer: loudness loss near hygiene)
    "bass_overlap": 0.1,  # Mosaikbox spectral; round 15: both decks' lows near full, per beat
    "low_continuity": 0.1,  # round 15: the summed low band against a steady low end (the bass_overlap share)
    "early_handover": 0.2,  # round 15: the maintainer's rule one, never take the bass before the new one is there
    "seam_clicks": 0.1,  # PR #3873 v0 headroom analog
}

# ----- measurement parameters ------------------------------------------------
#: A beat is timed only where each deck's low band is within this of its own
#: loudest moment in the render; a deck faded or EQ'd further down than that
#: has no onset to time, only noise.
ONSET_PRESENCE_DB = -40.0
#: Round 7 coverage rule (replaces round 6's 8 in every quarter): alignment is measured when at
#: least MIN_TIMED_BEATS are timed in all and MIN_COVERED_QUARTERS quarters hold at least
#: MIN_BEATS_PER_COVERED_QUARTER of them.
MIN_TIMED_BEATS = 16
MIN_COVERED_QUARTERS = 3
MIN_BEATS_PER_COVERED_QUARTER = 2
#: The kick-to-incoming correlation peak must be this many times its best rival
#: more than an eighth of a beat away for the beat to be timed (round 1).
ONSET_DOMINANCE = 1.5
#: Round 10 (F3): beats whose kick band is absent on a deck are timed on this band instead.
FALLBACK_BAND_HZ = (2000.0, 8000.0)
#: Hats repeat every half beat or faster, so a lag beyond an eighth of a beat on the 2-8 kHz band is a
#: neighboring hat, not an offset (round 10: the smoke pair read 234 ms, half a beat, with a wider reach).
HIGH_BAND_LAG_BEATS = 0.125
#: Outgoing downbeats that must be read from the audio before a cut is timed against them.
MIN_DOWNBEATS_READ = 4
#: Bins per beat in the bar-folded accent profile.
PHASE_BINS_PER_BEAT = 16
#: The best whole-beat lag must beat the runner-up by this share, or the bar
#: accent is too flat to say where beat 1 is.
BAR_ACCENT_MARGIN = 0.1
#: Round 15 replaced this reading (file 15's): a bar counted as shared low end when both decks' low band was within
#: 20 dB of its own solo level. the maintainer's standard handoff sits 8.7 dB down and tripped it on every crossover bar; it is
#: still reported for one round as ``bass_overlap_bars_v0`` so the r3-to-r4 set can show both.
BASS_ACTIVE_DB_V0 = -20.0
#: Round 15, derived from the maintainer's hand rule: a beat where BOTH decks' lows are within this of their own solo level is
#: two basslines at once. His crossover never enters it (at its midpoint both sit near -4.3 dB). The comb filter of two
#: kicks a few ms apart is the beat-offset veto's, so there is no phase term here.
BASS_BOTH_FULL_DB = -3.0
#: The low band both readings use (the band the shared-low-end reading always used; under the engine's 250 Hz shelf).
LOW_BAND_HZ = (20.0, 150.0)
#: Vocal activity: stem RMS above this (spec 10, Mosaikbox), 50 ms frames.
VOCAL_ACTIVE_DBFS = -40.0
VOCAL_FRAME_S = 0.05
#: A planned step counts as found when the level changes by at least this.
STEP_MIN_DB = 6.0
STEP_EDGE_S = 0.01
#: A click's second difference stands this far above its surroundings...
CLICK_RATIO = 20.0
#: ...and this far above the same spot in the neighboring beats.
CLICK_ANOMALY = 4.0
#: Short-term loudness is read this far inside each solo span.
SOLO_GUARD_S = 1.5


@dataclass(frozen=True)
class Step:
    """A planned step change: ``deck``'s level jumps at ``at_s`` on the render clock."""

    at_s: float
    deck: str  # "out" | "in"


@dataclass(frozen=True)
class RenderContext:
    """What the plan meant, on the render's clock (seconds from its first frame)."""

    sample_rate: int
    beat_s: float
    overlap_s: tuple[float, float]
    out_downbeats_s: tuple[float, ...]
    #: Every tempo ratio the decks played at, from the renderer's telemetry; required, never assumed.
    tempo_ratios: tuple[float, ...]
    #: The plan's parsed intent (one of ``apps.autodj.propose.INTENTS``), or None when the plan states none. Required,
    #: never assumed: only an explicit ``wind_down`` skips the loudness-loss veto (round 14).
    intent: str | None
    #: The plan's ``shape`` (``LULL_THEN_DROP`` or another proposer shape), or None when the plan carries none; required
    #: like ``intent``, so an exemption is always read from the plan, never defaulted.
    shape: str | None
    steps: tuple[Step, ...] = ()
    seams_s: tuple[float, ...] = ()
    pitch_follows_tempo: bool = True
    vocal_out: np.ndarray | None = None
    vocal_in: np.ndarray | None = None
    vocal_unknown_reason: str = "no vocal stem: the renderer does not load stems (plan 20-04)"
    parity: tuple[float, float] | None = None
    parity_unknown_reason: str = "no live capture of this plan yet (SET-01 parity capture)"
    #: The outgoing track's own audio over the render span, unprocessed (no fader, EQ or filter), at ``sample_rate``:
    #: the level the transition is held to (round 14). The deck output cannot be: it is post-fader.
    out_natural: np.ndarray | None = None
    out_natural_unknown_reason: str = "no outgoing source audio on the render clock"
    #: Where the incoming bass lands on the render clock (the plan's ``bass_entry.hit_plan_bar``, #7818 D11), or None.
    bass_hit_s: float | None = None
    bass_hit_unknown_reason: str = "the plan names no bass entry (bass_entry.hit_plan_bar)"
    #: Round 15: where the incoming track's own bass enters on the render clock, from its profile's bar_bass (content,
    #: never the plan), or None; and the incoming source on the render clock (placed at its play, tempo-matched).
    in_bass_entry_s: float | None = None
    in_bass_entry_unknown_reason: str = "no incoming bass entry from the profile (bar_bass)"
    in_natural: np.ndarray | None = None

    def __post_init__(self) -> None:
        start, end = self.overlap_s
        if not (0.0 <= start < end):
            raise ValueError(f"overlap must satisfy 0 <= start < end, got {self.overlap_s}")
        if self.beat_s <= 0:
            raise ValueError(f"beat_s must be positive, got {self.beat_s}")
        if self.intent is not None and self.intent not in INTENTS:
            raise ValueError(f"intent must be one of {INTENTS} or None, got {self.intent!r}")


@dataclass
class _Tally:
    metrics: dict[str, object] = field(default_factory=dict)
    factors: dict[str, float] = field(default_factory=dict)
    vetoes: list[Veto] = field(default_factory=list)
    unknown: dict[str, str] = field(default_factory=dict)

    def veto_if(self, name: str, value: float, threshold: float, fired: bool) -> None:
        if fired:
            self.vetoes.append(Veto(name=name, value=round(float(value), 4), threshold=threshold))


def squash(error: float, knee: float) -> float:
    """1.0 at zero error, 0.5 at the knee, falling toward 0 beyond it."""
    if error < 0 or knee <= 0:
        raise ValueError(f"squash needs error >= 0 and knee > 0, got {error}, {knee}")
    return 1.0 / (1.0 + (error / knee) ** 2)


# ----- alignment ---------------------------------------------------------------


def _beat_times(ctx: RenderContext, start: float, end: float) -> list[float]:
    """The outgoing deck's beats inside [start, end): downbeats subdivided into four."""
    beats: list[float] = []
    bars = ctx.out_downbeats_s
    for index, down in enumerate(bars):
        bar = bars[index + 1] - down if index + 1 < len(bars) else 4 * ctx.beat_s
        beats += [down + bar * k / 4.0 for k in range(4)]
    return [b for b in beats if start <= b < end]


def _interp_peak(values: np.ndarray, index: int) -> float:
    if 0 < index < len(values) - 1:
        left, mid, right = values[index - 1], values[index], values[index + 1]
        denom = left - 2 * mid + right
        if denom < 0:
            return index + 0.5 * (left - right) / denom
    return float(index)


def _dominant(values: np.ndarray, peak: int, guard: int) -> bool:
    """Whether ``values[peak]`` beats everything more than ``guard`` samples from it by ``ONSET_DOMINANCE``."""
    rest = np.concatenate([values[: max(0, peak - guard)], values[peak + guard + 1 :]])
    return float(values[peak]) > 0 and float(values[peak]) >= ONSET_DOMINANCE * float(rest.max(initial=0.0))


@dataclass(frozen=True)
class _Band:
    """One band's onsets and envelopes for both decks, with each deck's presence floor."""

    name: str
    out_onsets: np.ndarray
    in_onsets: np.ndarray
    out_env: np.ndarray
    in_env: np.ndarray

    def floors(self) -> tuple[float, float]:
        presence = 10.0 ** (ONSET_PRESENCE_DB / 20.0)
        return presence * float(self.out_env.max()), presence * float(self.in_env.max())


def _time_at(band: _Band, center: int, ctx: RenderContext, lag_beats: float = 0.5) -> tuple[str, float]:
    """Time one grid position on one band: ("absent", 0) when either deck has nothing in the band there,
    ("untimed", 0) when present but ambiguous, ("timed", ms) otherwise.

    The outgoing onset is looked for within a sixteenth of a beat of the position (round 6) and must be
    the dominant onset of the beat around it; only that onset is the template, cross-correlated with the
    incoming onsets over `lag_beats` either side (half a beat on the kick band), and the correlation must
    have one dominant peak that is not at the edge of that window.
    """
    hop_s = signals.ENVELOPE_HOP / ctx.sample_rate
    half = int(ctx.beat_s / 2 / hop_s)
    quarter = int(ctx.beat_s / 4 / hop_s)
    guard = int(ctx.beat_s / 8 / hop_s)
    kick = max(1, int(ctx.beat_s / 16 / hop_s))
    if center - half - guard - kick < 0 or center + half + guard + kick > min(
        len(band.in_onsets), len(band.out_onsets)
    ):
        return "absent", 0.0
    out_floor, in_floor = band.floors()
    window = slice(center - quarter, center + quarter)
    if (
        out_floor <= 0
        or in_floor <= 0
        or band.out_env[window].max() < out_floor
        or band.in_env[window].max() < in_floor
    ):
        return "absent", 0.0
    near = band.out_onsets[center - kick : center + kick]
    onset_at = center - kick + int(np.argmax(near))
    if not _dominant(band.out_onsets[center - half : center + half], onset_at - (center - half), guard):
        return "untimed", 0.0
    template = band.out_onsets[onset_at - kick : onset_at + kick]
    reach = int(ctx.beat_s * lag_beats / hop_s)
    corr = np.correlate(band.in_onsets[onset_at - reach - kick : onset_at + reach + kick], template, mode="valid")
    best = int(np.argmax(corr))  # index i: lag i - reach hops
    if best in (0, len(corr) - 1) or not _dominant(corr, best, min(guard, reach // 2)):
        return "untimed", 0.0
    return "timed", (_interp_peak(corr, best) - reach) * hop_s * 1000.0


def beat_offsets_ms(low: _Band, high: _Band | None, ctx: RenderContext) -> tuple[list[tuple[float, float, str]], int]:
    """((beat s, incoming lag behind the outgoing deck in ms, band) per timed beat, beats considered).

    Each beat is timed on the 40-150 Hz kick band first (rounds 1 and 6: the outgoing kick alone, dominant
    in its beat, matched unambiguously). Round 10 (F3): when that band is ABSENT at a beat (a deck's lows
    killed, as in a bass swap, or below the presence floor), the beat is timed on the 2-8 kHz band with the
    same rules, at the beat or, failing that, at its off-beat (where hi-hats sit). A beat whose low band is
    present but ambiguous (a bass-led section) is NOT handed to the high band: the round 6 refusal stands.
    A beat is considered when either band is present on both decks.
    """
    hop_s = signals.ENVELOPE_HOP / ctx.sample_rate
    start, end = ctx.overlap_s
    offsets: list[tuple[float, float, str]] = []
    considered = 0
    for beat in _beat_times(ctx, start, end):
        center = round(beat / hop_s)
        status, ms = _time_at(low, center, ctx)
        if status != "absent":
            considered += 1
            if status == "timed":
                offsets.append((beat, ms, low.name))
            continue
        if high is None:
            continue
        tried = [
            _time_at(high, round((beat + shift) / hop_s), ctx, HIGH_BAND_LAG_BEATS) for shift in (0.0, ctx.beat_s / 2)
        ]
        if any(t != "absent" for t, _ in tried):
            considered += 1
        timed = [m for t, m in tried if t == "timed"]
        if timed:
            offsets.append((beat, timed[0], high.name))
    return offsets, considered


def bar_lag_beats(out_onsets: np.ndarray, in_onsets: np.ndarray, ctx: RenderContext) -> tuple[float | None, str]:
    """Whole beats the incoming bar accent sits behind the outgoing one, or None with why.

    Each deck's onsets are folded on the outgoing deck's bars (the outgoing
    deck over its solo span and the overlap, the incoming over the overlap and
    its solo span), and the two folds are circularly cross-correlated.
    """
    hop_s = signals.ENVELOPE_HOP / ctx.sample_rate
    bins = 4 * PHASE_BINS_PER_BEAT
    start, end = ctx.overlap_s
    downs = np.asarray(ctx.out_downbeats_s)
    if len(downs) < 2:
        return None, "fewer than two outgoing downbeats on the render clock"
    times = np.arange(len(out_onsets)) * hop_s

    def fold(onsets: np.ndarray, lo: float, hi: float) -> np.ndarray:
        mask = (times >= max(lo, float(downs[0]))) & (times < hi)
        t = times[mask]
        index = np.clip(np.searchsorted(downs, t, side="right") - 1, 0, len(downs) - 2)
        phase = (t - downs[index]) / (downs[index + 1] - downs[index])
        hist = np.bincount((np.clip(phase, 0, 0.999999) * bins).astype(int), weights=onsets[mask], minlength=bins)
        return hist / (hist.sum() + 1e-12)

    out_fold = fold(out_onsets[: len(times)], 0.0, end)
    in_fold = fold(in_onsets[: len(times)], start, times[-1])
    if out_fold.sum() == 0 or in_fold.sum() == 0:
        return None, "no low-band onsets to fold on the bar"
    xcorr = np.fft.ifft(np.fft.fft(in_fold) * np.conj(np.fft.fft(out_fold))).real
    per_beat = [
        float(xcorr[(k * PHASE_BINS_PER_BEAT + d) % bins])
        for k in range(4)
        for d in range(-PHASE_BINS_PER_BEAT // 2, PHASE_BINS_PER_BEAT // 2)
    ]
    whole = [max(per_beat[k * PHASE_BINS_PER_BEAT : (k + 1) * PHASE_BINS_PER_BEAT]) for k in range(4)]
    ranked = sorted(whole, reverse=True)
    if ranked[0] <= 0 or (ranked[0] - ranked[1]) / ranked[0] < BAR_ACCENT_MARGIN:
        return None, "no bar accent: every beat of the bar sounds alike, so beat 1 cannot be told from the audio"
    lag = int(np.argmax(whole))
    return float(lag if lag <= 2 else lag - 4), ""


# ----- the measure ---------------------------------------------------------------


def _high_band(out_deck: np.ndarray, in_deck: np.ndarray, ctx: RenderContext) -> _Band:
    """The 2-8 kHz fallback band (round 10, F3): hats and claps carry the beat where the lows are killed."""
    envs = [signals.band_envelope(deck, ctx.sample_rate, FALLBACK_BAND_HZ) for deck in (out_deck, in_deck)]
    return _Band("high", signals.onset_envelope(envs[0]), signals.onset_envelope(envs[1]), envs[0], envs[1])


def _alignment_quarters(timed: list[tuple[float, float, str]], ctx: RenderContext) -> list[dict[str, Any]]:
    """Timed beats, p50 and p95 (ms) in each quarter of the overlap (round 6): a tail is placed, not averaged away."""
    start, end = ctx.overlap_s
    span = (end - start) / 4
    quarters: list[dict[str, Any]] = []
    for i in range(4):
        values = [
            ms for beat, ms, _ in timed if start + i * span <= beat < start + (i + 1) * span or (i == 3 and beat == end)
        ]
        quarters.append(
            {
                "timed": len(values),
                "p50_ms": round(float(np.percentile(values, 50)), 2) if values else None,
                "p95_ms": round(float(np.percentile(np.abs(values), 95)), 2) if values else None,
            }
        )
    return quarters


def alignment_coverage_gap(quarters: list[dict[str, Any]]) -> str | None:
    """Why the timed beats cannot stand for the overlap, or None when they can (round 7).

    Measured when at least ``MIN_TIMED_BEATS`` beats are timed in all AND at
    least ``MIN_COVERED_QUARTERS`` quarters hold ``MIN_BEATS_PER_COVERED_QUARTER``:
    enough beats for a p95 that is not just the worst beat, spread over the
    overlap rather than bunched in one stretch. Round 6's 8 per quarter
    overshot: it refused a bass-heavy exit that timed 26 beats at a steady
    -7 ms just as it refused one that timed 3.
    """
    counts = [q["timed"] for q in quarters]
    covered = sum(1 for c in counts if c >= MIN_BEATS_PER_COVERED_QUARTER)
    if sum(counts) >= MIN_TIMED_BEATS and covered >= MIN_COVERED_QUARTERS:
        return None
    return (
        f"{sum(counts)} beats timed across the overlap quarters {counts}; needs {MIN_TIMED_BEATS} in all and "
        f"{MIN_BEATS_PER_COVERED_QUARTER} in at least {MIN_COVERED_QUARTERS} of 4 quarters (a beat is timed when the "
        "outgoing kick dominates its beat and the incoming match is unambiguous)"
    )


def _alignment(t: _Tally, envelopes: tuple[np.ndarray, np.ndarray], high: _Band | None, ctx: RenderContext) -> None:
    """Per-beat timing and the bar accent, both on linear onsets (a correlation peak does not care how loud a deck is)."""
    out_env, in_env = envelopes
    out_onsets, in_onsets = (signals.onset_envelope(env) for env in envelopes)
    low = _Band("low", out_onsets, in_onsets, out_env, in_env)
    timed, considered = beat_offsets_ms(low, high, ctx)
    offsets = [ms for _, ms, _ in timed]
    quarters = _alignment_quarters(timed, ctx)
    t.metrics.update(
        beats_considered=considered,
        beats_measured=len(offsets),
        beats_low=sum(1 for *_, band in timed if band == "low"),
        beats_high=sum(1 for *_, band in timed if band == "high"),
        alignment_quarters=quarters,
    )
    why_not = alignment_coverage_gap(quarters)
    if why_not is not None:
        t.unknown["alignment"] = why_not
    else:
        magnitude = np.abs(np.asarray(offsets))
        p50, p95 = float(np.percentile(np.asarray(offsets), 50)), float(np.percentile(magnitude, 95))
        t.metrics.update(beat_offset_ms_p50=round(p50, 2), beat_offset_ms_p95=round(p95, 2))
        t.factors["alignment"] = squash(p95, ALIGNMENT_KNEE_MS)
        t.veto_if("beat_offset_p95", p95, BEAT_OFFSET_P95_VETO_MS, p95 > BEAT_OFFSET_P95_VETO_MS)
    lag, why = bar_lag_beats(out_onsets, in_onsets, ctx)
    if lag is None:
        t.unknown["downbeat_phase"] = why
        return
    t.metrics["bar_lag_beats"] = lag
    t.factors["downbeat_phase"] = 1.0 if lag == 0 else 0.0
    t.veto_if("bar_lag", lag, 0.0, lag != 0)


def _solo_spans(ctx: RenderContext, length_s: float) -> tuple[tuple[float, float], tuple[float, float]]:
    start, end = ctx.overlap_s
    return (0.0, start), (end, length_s)


def _true_peak(t: _Tally, mix: np.ndarray, decks: tuple[np.ndarray, np.ndarray], ctx: RenderContext) -> None:
    """Round 15: the overlap's true peak against the louder SINGLE deck over the same overlap, so the veto measures
    what the transition adds (r3: the outgoing at full until the hit peaks about 1 dB over its own lead-in)."""
    sr = ctx.sample_rate
    span = slice(int(ctx.overlap_s[0] * sr), int(ctx.overlap_s[1] * sr))
    overlap_peak = signals.true_peak_dbtp(mix[span])
    reference = max(signals.true_peak_dbtp(deck[span]) for deck in decks)
    t.metrics.update(true_peak_dbtp=round(overlap_peak, 2), deck_true_peak_dbtp=round(reference, 2))
    fired = overlap_peak > TRUE_PEAK_VETO_DBTP and overlap_peak > reference + TRUE_PEAK_EXCESS_DB
    t.veto_if("true_peak", overlap_peak, TRUE_PEAK_VETO_DBTP, fired)


def _dropout(t: _Tally, mix: np.ndarray, ctx: RenderContext) -> None:
    """Momentary (400 ms) loudness: a 3 s window cannot see a 0.5 s hole."""
    _, lufs = signals.loudness_series(mix, ctx.sample_rate, signals.MOMENTARY_S)
    if len(lufs) == 0:
        t.unknown["dropout"] = "render shorter than one 400 ms loudness window"
        return
    hop = signals.LOUDNESS_HOP_S
    longest = run = 0
    for quiet in lufs < DROPOUT_LUFS:
        run = run + 1 if quiet else 0
        longest = max(longest, run)
    seconds = longest * hop + (signals.MOMENTARY_S - hop if longest else 0.0)
    t.metrics["dropout_s"] = round(seconds, 2)
    t.veto_if("dropout", seconds, DROPOUT_VETO_S, seconds > DROPOUT_VETO_S)


def _tempo(t: _Tally, ctx: RenderContext) -> None:
    if not ctx.tempo_ratios:
        for name in ("stretch", "key_shift"):
            t.unknown[name] = "no tempo telemetry from the renderer"
        return
    stretch = max(abs(r - 1.0) for r in ctx.tempo_ratios) * 100.0
    t.metrics["stretch_pct"] = round(stretch, 3)
    t.veto_if("stretch", stretch, STRETCH_VETO_PCT, stretch > STRETCH_VETO_PCT)
    if not ctx.pitch_follows_tempo:
        t.unknown["key_shift"] = "Master Tempo was on: the stretch kept pitch, so no key shift to read"
        return
    shift = max(abs(12.0 * math.log2(r)) for r in ctx.tempo_ratios)
    t.metrics["key_shift_st"] = round(shift, 3)
    t.veto_if("key_shift", shift, KEY_SHIFT_VETO_ST, shift > KEY_SHIFT_VETO_ST)


def _parity(t: _Tally, ctx: RenderContext) -> None:
    if ctx.parity is None:
        t.unknown["parity"] = ctx.parity_unknown_reason
        return
    t.vetoes.extend(parity_vetoes(*ctx.parity))
    t.metrics.update(parity_beat_ms=ctx.parity[0], parity_lu=ctx.parity[1])


def parity_vetoes(beat_offset_delta_ms: float, loudness_delta_lu: float) -> list[Veto]:
    """Live capture vs render: beat offset within 5 ms and short-term LUFS within 0.5 LU (spec 10 trap 2)."""
    vetoes: list[Veto] = []
    if abs(beat_offset_delta_ms) > PARITY_VETO_MS:
        vetoes.append(Veto("parity_beat_ms", round(abs(beat_offset_delta_ms), 3), PARITY_VETO_MS))
    if abs(loudness_delta_lu) > PARITY_VETO_LU:
        vetoes.append(Veto("parity_lu", round(abs(loudness_delta_lu), 3), PARITY_VETO_LU))
    return vetoes


def _loudness(t: _Tally, mix: np.ndarray, ctx: RenderContext) -> None:
    centers, lufs = signals.loudness_series(mix, ctx.sample_rate, signals.SHORT_TERM_S)
    (a0, a1), (b0, b1) = _solo_spans(ctx, len(mix) / ctx.sample_rate)
    half = signals.SHORT_TERM_S / 2
    pre = (centers >= a0 + half) & (centers <= a1 - SOLO_GUARD_S)
    post = (centers >= b0 + SOLO_GUARD_S) & (centers <= b1 - half)
    start, end = ctx.overlap_s
    during = (centers >= start) & (centers <= end)
    if not pre.any() or not post.any() or not during.any():
        t.unknown["loudness_continuity"] = (
            f"needs a solo span of at least {signals.SHORT_TERM_S + SOLO_GUARD_S:.1f} s on each side of the overlap"
        )
        return
    level_a, level_b = float(np.median(lufs[pre])), float(np.median(lufs[post]))
    line = level_a + (level_b - level_a) * (centers[during] - start) / (end - start)
    deviation = lufs[during] - line
    gap = float(np.abs(deviation).max())
    t.metrics.update(
        loudness_gap_lu=round(gap, 2),
        loudness_bump_lu=round(float(deviation.max()), 2),
        loudness_dip_lu=round(float(-deviation.min()), 2),
        out_solo_lufs=round(level_a, 2),
        in_solo_lufs=round(level_b, 2),
    )
    t.factors["loudness_continuity"] = squash(gap, LOUDNESS_KNEE_LU)
    _loudness_loss(t, centers, lufs, pre, level_a, ctx)


def longest_run_s(below: np.ndarray, hop_s: float) -> float:
    """Seconds of the longest unbroken run of True in ``below`` (one reading per ``hop_s``)."""
    longest = run = 0
    for flag in below:
        run = run + 1 if flag else 0
        longest = max(longest, run)
    return longest * hop_s


def _natural_reference(
    t: _Tally, centers: np.ndarray, pre: np.ndarray, level_out: float, ctx: RenderContext
) -> np.ndarray | None:
    """The outgoing track's own short-term loudness on the mix's clock, offset to match the mix over the outgoing solo
    span (the render's gain staging cancels), or None with the reason in ``unknown``."""
    if ctx.out_natural is None:
        t.unknown["loudness_loss"] = ctx.out_natural_unknown_reason
        return None
    _, natural = signals.loudness_series(ctx.out_natural, ctx.sample_rate, signals.SHORT_TERM_S)
    if len(natural) != len(centers):
        raise ValueError(f"outgoing source gives {len(natural)} loudness readings, the mix {len(centers)}")
    return natural + (level_out - float(np.median(natural[pre])))


def _recovery(t: _Tally, centers: np.ndarray, lufs: np.ndarray, level_out: float, ctx: RenderContext) -> None:
    """Round 14: how far under the outgoing's solo level the mix still is in the bar after the incoming bass hit
    (``loudness_recovery_gap_lu``; ``loudness_recovered`` within RECOVERY_TOLERANCE_LU). The step back up there is the
    shape a lull is meant to have, so it is reported beside the loss, never scored as a bump."""
    if ctx.bass_hit_s is None:
        t.unknown["loudness_recovery"] = ctx.bass_hit_unknown_reason
        return
    after = (centers >= ctx.bass_hit_s) & (centers <= ctx.bass_hit_s + 4.0 * ctx.beat_s)
    if not after.any():
        t.unknown["loudness_recovery"] = f"no loudness reading in the bar after the bass hit at {ctx.bass_hit_s:.2f} s"
        return
    gap = max(0.0, level_out - float(lufs[after].max()))
    t.metrics.update(loudness_recovery_gap_lu=round(gap, 2), loudness_recovered=gap <= RECOVERY_TOLERANCE_LU)


def _exemption(ctx: RenderContext) -> str | None:
    if ctx.intent == WIND_DOWN:
        return "wind_down intent: an energy drop is what the plan asks for"
    if ctx.shape == LULL_THEN_DROP:
        return "lull_then_drop shape: the outgoing's lull is held on purpose until the incoming drop lands"
    return None


def _loudness_loss(
    t: _Tally, centers: np.ndarray, lufs: np.ndarray, pre: np.ndarray, level_out: float, ctx: RenderContext
) -> None:
    """Round 14: how long the mix's short-term loudness (the loudness_continuity series) stays more than
    LOUDNESS_LOSS_VETO_LU under what the outgoing track would be playing on its own at that moment. Against the
    outgoing's own envelope, not a line between the solo levels: a breakdown in the outgoing is the track, not loss;
    and not the incoming's: a naturally louder incoming fading in is a blend, not loss."""
    reference = _natural_reference(t, centers, pre, level_out, ctx)
    if reference is None:
        return
    start, end = ctx.overlap_s
    during = (centers >= start) & (centers <= end)
    deficit = reference[during] - lufs[during]
    bar_s = 4.0 * ctx.beat_s
    bars = longest_run_s(deficit > LOUDNESS_LOSS_VETO_LU, signals.LOUDNESS_HOP_S) / bar_s
    fires = bars > LOUDNESS_LOSS_VETO_BARS
    held = max(1, round(LOUDNESS_LOSS_VETO_BARS * bar_s / signals.LOUDNESS_HOP_S))
    # The deepest loss held for the veto's whole span: how deep a hole is, beside how long (round 14).
    depth = max(0.0, float(sliding_window_view(deficit, held).min(axis=1).max())) if len(deficit) >= held else 0.0
    t.metrics.update(
        loudness_loss_bars=round(bars, 2), loudness_loss_depth_lu=round(depth, 2), intent=ctx.intent, shape=ctx.shape
    )
    _recovery(t, centers, lufs, level_out, ctx)
    exempt = _exemption(ctx)
    if exempt is not None:
        t.metrics["loudness_loss_exempt"] = exempt
        return
    t.metrics["loudness_loss_would_veto"] = fires
    if LOUDNESS_LOSS_VETO_ON:
        t.veto_if("loudness_loss", bars, LOUDNESS_LOSS_VETO_BARS, fires)


def loudness_checks(mix: np.ndarray, ctx: RenderContext) -> ScoreResult:
    """The mix-only loudness terms of :func:`score_render` (loudness_continuity and the loudness-loss veto), for a
    re-score that has the mix but not the deck outputs (round 14: oltf-r2 kept clips without deck slices). The
    result carries those terms only; its ``score`` is None, never a composite."""
    t = _Tally()
    _loudness(t, mix, ctx)
    measured = Measured(k=sum(1 for name in CORE_FACTORS if name in t.factors), n=len(CORE_FACTORS))
    return ScoreResult(t.metrics, t.factors, tuple(t.vetoes), measured, t.unknown, None, SCORER_VERSION)


def _bar_low_db(deck: np.ndarray, sr: int, edges: list[tuple[float, float]]) -> np.ndarray:
    env = signals.band_envelope(deck, sr, LOW_BAND_HZ)
    hop_s = signals.ENVELOPE_HOP / sr
    return np.array(
        [20.0 * math.log10(max(float(env[int(a / hop_s) : int(b / hop_s)].mean()), 1e-10)) for a, b in edges]
    )


def _bars_between(ctx: RenderContext, lo: float, hi: float) -> list[tuple[float, float]]:
    downs = ctx.out_downbeats_s
    return [(a, b) for a, b in itertools.pairwise(downs) if a >= lo - 1e-6 and b <= hi + 1e-6]


def _beats_between(ctx: RenderContext, lo: float, hi: float) -> list[tuple[tuple[float, float], int]]:
    """((start, end), position in the bar 0..3) of every whole outgoing beat inside [lo, hi]."""
    found = []
    for index, down in enumerate(ctx.out_downbeats_s):
        bar = ctx.out_downbeats_s[index + 1] - down if index + 1 < len(ctx.out_downbeats_s) else 4 * ctx.beat_s
        for k in range(4):
            a = down + bar * k / 4.0
            if a >= lo - 1e-6 and a + bar / 4.0 <= hi + 1e-6:
                found.append(((a, a + bar / 4.0), k))
    return found


def _beat_levels(low: np.ndarray, sr: int, beats: list[tuple[tuple[float, float], int]]) -> np.ndarray:
    """Low-band POWER per beat (``low`` is the band-passed signal): the summed low end adds as power."""
    return signals.span_power_db(low, sr, [edges for edges, _ in beats])


def _by_position(levels: np.ndarray, beats: list[tuple[tuple[float, float], int]]) -> np.ndarray:
    """The median low level at each of the 4 beat positions (a kick on 1 is louder than on 3; each beat is held to
    its own position's level, so a bar's accent pattern is not read as a hole or a pile-up)."""
    positions = np.array([k for _, k in beats])
    return np.array([float(np.median(levels[positions == k])) if (positions == k).any() else np.nan for k in range(4)])


def _bass_overlap(t: _Tally, mix: np.ndarray, out_deck: np.ndarray, in_deck: np.ndarray, ctx: RenderContext) -> None:
    """Round 15: two readings of the low-band handoff, both from the 20-150 Hz band, per beat, each beat held to the
    level at its own position in the bar on the solo spans.

    ``bass_overlap`` counts beats where BOTH decks' lows are within BASS_BOTH_FULL_DB of their own solo level (two
    basslines at once). ``low_continuity`` reads the MIX's summed low band against the straight line from its outgoing
    solo level to its incoming solo level, like loudness continuity: a hole (both lows down) and a pile-up (both near
    full) both show, a handoff that keeps the sum steady does not. Both decks partly present for a few bars, as the maintainer
    hands over, is neither.
    """
    sr, length = ctx.sample_rate, len(out_deck) / ctx.sample_rate
    (a0, a1), (b0, b1) = _solo_spans(ctx, length)
    overlap_bars, overlap_beats = _bars_between(ctx, *ctx.overlap_s), _beats_between(ctx, *ctx.overlap_s)
    out_solo_bars, in_solo_bars = _bars_between(ctx, a0, a1), _bars_between(ctx, b0, b1)
    out_solo, in_solo = _beats_between(ctx, a0, a1), _beats_between(ctx, b0, b1)
    positions = np.array([k for _, k in overlap_beats])
    covered = {k for _, k in out_solo} == {k for _, k in in_solo} == set(range(4))
    if not overlap_bars or not overlap_beats or not out_solo_bars or not in_solo_bars or not covered:
        for name in ("bass_overlap", "low_continuity"):
            t.unknown[name] = "needs whole bars of each deck alone and of the overlap on the outgoing grid"
        return
    out_ref_v0 = float(np.median(_bar_low_db(out_deck, sr, out_solo_bars)))
    in_ref_v0 = float(np.median(_bar_low_db(in_deck, sr, in_solo_bars)))
    out_v0 = _bar_low_db(out_deck, sr, overlap_bars) - out_ref_v0
    in_v0 = _bar_low_db(in_deck, sr, overlap_bars) - in_ref_v0
    t.metrics["bass_overlap_bars_v0"] = int(np.sum((out_v0 > BASS_ACTIVE_DB_V0) & (in_v0 > BASS_ACTIVE_DB_V0)))
    out_low, in_low, mix_low = (signals.band_signal(x, sr, LOW_BAND_HZ) for x in (out_deck, in_deck, mix))
    out_ref = _by_position(_beat_levels(out_low, sr, out_solo), out_solo)[positions]
    in_ref = _by_position(_beat_levels(in_low, sr, in_solo), in_solo)[positions]
    out_beats = _beat_levels(out_low, sr, overlap_beats) - out_ref
    in_beats = _beat_levels(in_low, sr, overlap_beats) - in_ref
    both = int(np.sum((out_beats > BASS_BOTH_FULL_DB) & (in_beats > BASS_BOTH_FULL_DB)))
    t.metrics["bass_both_full_beats"] = both
    t.factors["bass_overlap"] = squash(float(both), BASS_BOTH_FULL_KNEE_BEATS)
    raw_out, raw_in = out_beats + out_ref, in_beats + in_ref
    switch = next((j for j in range(len(overlap_beats)) if raw_in[j] > raw_out[j]), len(overlap_beats))
    switch_s = overlap_beats[switch][0][0] if switch < len(overlap_beats) else ctx.overlap_s[1]
    t.metrics["low_switch_s"] = round(switch_s, 3)
    _early_handover(t, switch_s, ctx)
    _low_hole(t, mix_low, overlap_beats, switch, ctx)
    mix_a = _by_position(_beat_levels(mix_low, sr, out_solo), out_solo)[positions]
    mix_b = _by_position(_beat_levels(mix_low, sr, in_solo), in_solo)[positions]
    _low_continuity(t, _beat_levels(mix_low, sr, overlap_beats), mix_a, mix_b, overlap_beats, ctx)


def _early_handover(t: _Tally, switch_s: float, ctx: RenderContext) -> None:
    """Round 15: bars by which the incoming's low takes over (read from the deck slices) before its own content bass
    enters (its profile, never the plan). Zero at or after the entry; wind_down and lull_then_drop exempt."""
    if ctx.in_bass_entry_s is None:
        t.unknown["early_handover"] = ctx.in_bass_entry_unknown_reason
        return
    bars = max(0.0, (ctx.in_bass_entry_s - switch_s) / (4.0 * ctx.beat_s))
    t.metrics["early_handover_bars"] = round(bars, 2)
    exempt = _exemption(ctx)
    if exempt is not None:
        t.metrics["early_handover_exempt"] = exempt
        return
    t.factors["early_handover"] = squash(bars, EARLY_HANDOVER_KNEE_BARS)


def _low_hole(
    t: _Tally, mix_low: np.ndarray, beats: list[tuple[tuple[float, float], int]], switch: int, ctx: RenderContext
) -> None:
    """Round 15, REPORTED only: the mix low band against the dominant deck's own SOURCE low band (outgoing before the
    switch, incoming from it), each matched to the mix over its solo span."""
    if ctx.out_natural is None or ctx.in_natural is None:
        t.unknown["low_hole"] = "needs both decks' source audio on the render clock"
        return
    sr = ctx.sample_rate
    edges = [e for e, _ in beats]
    pre = [e for e, _ in _beats_between(ctx, 0.5, ctx.overlap_s[0])]
    post = [e for e, _ in _beats_between(ctx, ctx.overlap_s[1], len(mix_low) / sr - 0.5)]
    out_n, in_n = (signals.band_signal(x, sr, LOW_BAND_HZ) for x in (ctx.out_natural, ctx.in_natural))
    if not pre or not post:
        t.unknown["low_hole"] = "needs whole beats of each solo span"
        return
    off_out = float(np.median(signals.span_power_db(mix_low, sr, pre) - signals.span_power_db(out_n, sr, pre)))
    off_in = float(np.median(signals.span_power_db(mix_low, sr, post) - signals.span_power_db(in_n, sr, post)))
    ref = np.where(
        np.arange(len(edges)) < switch,
        signals.span_power_db(out_n, sr, edges) + off_out,
        signals.span_power_db(in_n, sr, edges) + off_in,
    )
    deficit = ref - signals.span_power_db(mix_low, sr, edges)
    t.metrics.update(
        low_hole_beats=int(longest_run_s(deficit > LOW_HOLE_DB, 1.0)),
        low_hole_depth_db=round(float(np.max(np.minimum(deficit[:-1], deficit[1:]))) if len(deficit) > 1 else 0.0, 2),
        low_hole_after_switch_db={
            str(k): round(float(deficit[switch + k - 1]), 2) for k in (1, 4, 8, 16) if switch + k - 1 < len(deficit)
        },
    )


def _low_continuity(
    t: _Tally,
    levels: np.ndarray,
    level_a: np.ndarray,
    level_b: np.ndarray,
    beats: list[tuple[tuple[float, float], int]],
    ctx: RenderContext,
) -> None:
    start, end = ctx.overlap_s
    centers = np.array([(a + b) / 2 for (a, b), _ in beats])
    line = level_a + (level_b - level_a) * (centers - start) / (end - start)
    deviation = levels - line
    gap = float(np.abs(deviation).max())
    off_beats = longest_run_s(np.abs(deviation) > LOW_RULE_EXCURSION_DB, 1.0)  # beyond what the maintainer's rule does
    t.metrics.update(
        low_gap_db=round(gap, 2),
        low_hole_db=round(max(0.0, float(-deviation.min())), 2),
        low_pile_db=round(max(0.0, float(deviation.max())), 2),
        low_off_bars=round(off_beats / 4.0, 2),
    )
    t.factors["low_continuity"] = squash(gap, LOW_CONTINUITY_KNEE_DB)
    if ctx.bass_hit_s is None:
        t.unknown["low_recovery"] = ctx.bass_hit_unknown_reason
        return
    after = (centers >= ctx.bass_hit_s) & (centers <= ctx.bass_hit_s + 4.0 * ctx.beat_s)
    if not after.any():
        t.unknown["low_recovery"] = f"no whole beat in the bar after the bass hit at {ctx.bass_hit_s:.2f} s"
        return
    # Credited, not scored as a bump: the low end stepping back to its line within a bar of the incoming bass hit is
    # the handoff landing (reported beside the loudness recovery).
    t.metrics["low_recovery_gap_db"] = round(max(0.0, float(-deviation[after].max())), 2)


def _vocal_overlap(t: _Tally, ctx: RenderContext) -> None:
    if ctx.vocal_out is None or ctx.vocal_in is None:
        t.unknown["vocal_overlap"] = ctx.vocal_unknown_reason
        return
    out_db = signals.frame_rms_db(ctx.vocal_out, ctx.sample_rate, VOCAL_FRAME_S)
    in_db = signals.frame_rms_db(ctx.vocal_in, ctx.sample_rate, VOCAL_FRAME_S)
    frames = min(len(out_db), len(in_db))
    seconds = (
        float(np.sum((out_db[:frames] > VOCAL_ACTIVE_DBFS) & (in_db[:frames] > VOCAL_ACTIVE_DBFS))) * VOCAL_FRAME_S
    )
    t.metrics["vocal_overlap_s"] = round(seconds, 2)
    t.factors["vocal_overlap"] = squash(seconds, VOCAL_OVERLAP_KNEE_S)


def _step_time(deck: np.ndarray, sr: int, at_s: float, search_s: float) -> float | None:
    """Where the deck's level jumps nearest ``at_s``, or None when no jump of STEP_MIN_DB is there."""
    frame_s = 0.001
    offset_s = max(0.0, at_s - search_s - 2 * STEP_EDGE_S)
    local = deck[int(offset_s * sr) : int((at_s + search_s + 2 * STEP_EDGE_S) * sr)]
    db = signals.level_db(local, sr, frame_s)
    edge = int(STEP_EDGE_S / frame_s)
    at_s -= offset_s
    lo, hi = max(edge, int((at_s - search_s) / frame_s)), min(len(db) - edge, int((at_s + search_s) / frame_s))
    if hi <= lo:
        return None
    jumps = np.array([abs(db[i : i + edge].mean() - db[i - edge : i].mean()) for i in range(lo, hi)])
    best = int(np.argmax(jumps))
    return None if jumps[best] < STEP_MIN_DB else offset_s + (lo + best) * frame_s


def downbeat_lag_s(out_deck: np.ndarray, ctx: RenderContext) -> float | None:
    """How late the outgoing deck's downbeats sound after its grid, or None when too few can be read.

    Read with the same level-step detector that times a cut (a kick's attack
    is a level step), at every grid downbeat at least a beat away from any
    planned step, and summarized by the median; so the target a cut is held to
    is the downbeat as rendered, and the detector's own bias cancels.
    """
    lags = []
    for down in ctx.out_downbeats_s:
        if any(abs(down - step.at_s) < ctx.beat_s for step in ctx.steps):
            continue
        if down - ctx.beat_s < 0 or (down + ctx.beat_s) * ctx.sample_rate > len(out_deck):
            continue
        found = _step_time(out_deck, ctx.sample_rate, down, ctx.beat_s / 8)
        if found is not None:
            lags.append(found - down)
    return float(np.median(lags)) if len(lags) >= MIN_DOWNBEATS_READ else None


def _cut_to_downbeat(t: _Tally, decks: dict[str, np.ndarray], ctx: RenderContext) -> None:
    """How far each step change lands from the downbeat that actually sounds (round 2).

    The planned time only says where to look: a plan that schedules a cut off
    the bar and renders it faithfully is still a cut off the bar. The target is
    nearest grid downbeat moved by how late the outgoing downbeats actually
    sound (:func:`downbeat_lag_s`); when they cannot be read, the factor is unknown.
    """
    if not ctx.steps:
        t.unknown["cut_to_downbeat"] = "not applicable: the plan makes no step change"
        return
    lag = downbeat_lag_s(decks["out"], ctx)
    if lag is None or not ctx.out_downbeats_s:
        t.unknown["cut_to_downbeat"] = (
            f"fewer than {MIN_DOWNBEATS_READ} outgoing downbeats away from the steps can be read from the audio"
        )
        return
    t.metrics["downbeat_lag_ms"] = round(lag * 1000.0, 2)
    errors = []
    for step in ctx.steps:
        found = _step_time(decks[step.deck], ctx.sample_rate, step.at_s, ctx.beat_s / 4)
        if found is not None:
            downbeat = min(ctx.out_downbeats_s, key=lambda d: abs(d - found)) + lag
            errors.append(abs(found - downbeat) * 1000.0)
    if not errors:
        t.unknown["cut_to_downbeat"] = (
            f"none of the {len(ctx.steps)} planned steps changes the level by {STEP_MIN_DB} dB"
        )
        return
    worst = max(errors)
    t.metrics.update(cut_error_ms_max=round(worst, 2), steps_measured=len(errors))
    t.factors["cut_to_downbeat"] = squash(worst, CUT_KNEE_MS)


#: The anomaly reported for a sound at a seam whose neighboring beats are all silent: finite (JSON carries it),
#: and far past CLICK_ANOMALY, so the seam counts as a click whenever its own ratio does.
SILENT_REFERENCE_ANOMALY = 1e6


def _seam_anomaly(mix: np.ndarray, ctx: RenderContext, seam_s: float) -> tuple[float, float]:
    """(click ratio at the seam, that ratio over the same spot one to four beats either side).

    A kick's attack is sharp too, but it is sharp on every beat; a seam click
    is sharp only at the seam. Comparing the seam with the same position in
    the neighboring beats keeps the music's own transients from reading as
    clicks (file 13: an anomaly at the wrap, not a transient).
    """
    length_s = len(mix) / ctx.sample_rate
    here = signals.click_ratio(mix, ctx.sample_rate, seam_s)
    refs = [
        signals.click_ratio(mix, ctx.sample_rate, seam_s + k * ctx.beat_s)
        for k in (-4, -3, -2, -1, 1, 2, 3, 4)
        if 0.2 < seam_s + k * ctx.beat_s < length_s - 0.2
    ]
    if not refs:
        raise ValueError(f"no neighboring beats around the seam at {seam_s:.3f} s")
    reference = float(np.median(refs))
    if reference > 0:
        return here, here / reference
    # Silent neighbors: silence at the seam is no click; anything audible there stands alone against silence.
    return here, (SILENT_REFERENCE_ANOMALY if here > 0 else 0.0)


def _seam_clicks(t: _Tally, mix: np.ndarray, ctx: RenderContext) -> None:
    seams = sorted(set(ctx.seams_s) | {s.at_s for s in ctx.steps})
    if not seams:
        t.unknown["seam_clicks"] = "not applicable: the plan has no cut, loop wrap or deck start to click"
        return
    found = [_seam_anomaly(mix, ctx, s) for s in seams]
    clicks = sum(1 for ratio, anomaly in found if ratio > CLICK_RATIO and anomaly > CLICK_ANOMALY)
    t.metrics.update(
        seam_clicks=clicks,
        seam_click_ratio_max=round(max(r for r, _ in found), 1),
        seam_click_anomaly_max=round(max(a for _, a in found), 1),
        seams_checked=len(seams),
    )
    t.factors["seam_clicks"] = squash(float(clicks), SEAM_CLICKS_KNEE)


def composite(factors: dict[str, float], weights: dict[str, float] = PRIOR_WEIGHTS) -> float:
    """Weighted geometric mean: one bad factor pulls the whole measure down hard."""
    total = sum(weights[name] for name in factors)
    return math.exp(sum(weights[name] * math.log(max(value, 1e-6)) for name, value in factors.items()) / total)


def score_render(mix: np.ndarray, out_deck: np.ndarray, in_deck: np.ndarray, ctx: RenderContext) -> ScoreResult:
    """M for one render: layer-0 vetoes, layer-1 factors, k of n measured, the unknown map."""
    if not (len(mix) == len(out_deck) == len(in_deck)):
        raise ValueError(f"mix and deck outputs differ in length: {len(mix)}, {len(out_deck)}, {len(in_deck)}")
    if ctx.out_natural is not None and len(ctx.out_natural) != len(mix):
        raise ValueError(f"outgoing source is {len(ctx.out_natural)} frames, the mix {len(mix)}")
    if ctx.overlap_s[1] > len(mix) / ctx.sample_rate:
        raise ValueError("the overlap runs past the end of the render")
    t = _Tally()
    envelopes = (signals.band_envelope(out_deck, ctx.sample_rate), signals.band_envelope(in_deck, ctx.sample_rate))
    _alignment(t, envelopes, _high_band(out_deck, in_deck, ctx), ctx)
    _true_peak(t, mix, (out_deck, in_deck), ctx)
    _dropout(t, mix, ctx)
    _tempo(t, ctx)
    _parity(t, ctx)
    _loudness(t, mix, ctx)
    _bass_overlap(t, mix, out_deck, in_deck, ctx)
    _vocal_overlap(t, ctx)
    _cut_to_downbeat(t, {"out": out_deck, "in": in_deck}, ctx)
    _seam_clicks(t, mix, ctx)
    measured = Measured(k=sum(1 for name in CORE_FACTORS if name in t.factors), n=len(CORE_FACTORS))
    if t.vetoes:
        score: float | None = 0.0
    elif measured.k < MIN_CORE_MEASURED:
        score = None
        t.unknown["score"] = (
            f"{measured.k} of {measured.n} core factors measured; a composite needs {MIN_CORE_MEASURED}"
        )
    else:
        score = round(composite(t.factors), 4)
    return ScoreResult(
        metrics=t.metrics,
        factors={k: round(v, 4) for k, v in t.factors.items()},
        vetoes=tuple(t.vetoes),
        measured=measured,
        unknown=t.unknown,
        score=score,
        scorer_version=SCORER_VERSION,
    )
