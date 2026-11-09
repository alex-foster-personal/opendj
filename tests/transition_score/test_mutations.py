"""Mutation tests of the measure M (TRYER-01, file 15 section 3.4): every check must be shown able to fail.

Each mutation injects one defect into a synthetic two-deck render
(:mod:`tests.transition_score.synth`, a fixture of the renderer's output, never
a fake of the scorer) and asserts M drops by more than the clean fixture's own
noise band. The control tests run the other direction: the clean render keeps
M high, and a mutation moves only the metric it targets.
"""

from __future__ import annotations

import dataclasses
from functools import cache
from typing import Any

import numpy as np
import pytest

from apps.transition_score import signals
from apps.transition_score.contract import CORE_FACTORS, SCORER_VERSION, ScoreResult
from apps.transition_score.scorer import (
    ALIGNMENT_KNEE_MS,
    BAND_FACTORS,
    BEAT_OFFSET_P95_VETO_MS,
    CUT_KNEE_MS,
    LOUDNESS_KNEE_LU,
    LOUDNESS_LOSS_VETO_BARS,
    LOUDNESS_LOSS_VETO_ON,
    PRIOR_WEIGHTS,
    RenderContext,
    score_render,
)
from tests.transition_score.synth import BEAT_S, Mutation, render

#: The clean fixture must score at least this; below it the measure is penalizing a good transition.
CLEAN_FLOOR = 0.8
#: Seeds that vary the fixture's noise and hats; M's spread across them is the noise band.
NOISE_SEEDS = (0, 1, 2, 3)
#: A drop smaller than this is not a drop, whatever the noise band says.
MIN_DROP = 0.02


@cache
def _score(mutation: Mutation | None = None, seed: int = 0) -> ScoreResult:
    return score_render(*render(mutation, seed))


@cache
def _noise_band() -> float:
    scores = [_score(Mutation(), seed).score for seed in NOISE_SEEDS]
    assert all(s is not None for s in scores), f"clean fixture unscored: {scores}"
    return max(scores) - min(scores)  # type: ignore[type-var,operator]


def _clean() -> float:
    score = _score().score
    assert score is not None
    return score


# ----- controls: the clean render ----------------------------------------------


def test_clean_render_scores_high_with_every_core_factor_measured() -> None:
    result = _score()
    print(f"clean M={result.score} measured={result.measured} factors={result.factors}")
    assert result.vetoes == ()
    assert result.measured.k == result.measured.n == 7
    assert result.score is not None and result.score >= CLEAN_FLOOR, (
        "if the clean render scores low then M penalizes good transitions"
    )
    assert result.scorer_version == SCORER_VERSION == "0.10.0"
    for name in BAND_FACTORS:
        assert name in result.factors and result.factors[name] >= 0.95
    assert result.metrics["band_handover"] == "plan_step"


def test_noise_band_is_small_against_the_drops() -> None:
    band = _noise_band()
    print(f"noise band across seeds {NOISE_SEEDS}: {band:.4f}")
    assert band < 0.05, "if M moves this much between clean seeds then no mutation drop can be told from noise"


def test_parity_reports_unknown_without_a_capture_never_a_pass() -> None:
    result = _score()
    assert "parity" in result.unknown and "capture" in result.unknown["parity"]
    assert not any(v.name.startswith("parity") for v in result.vetoes)


# ----- mutations: each one must drop M ---------------------------------------------

MUTATIONS: dict[str, Mutation] = {
    "beat +15 ms": Mutation(in_shift_s=0.015),
    "beat +30 ms": Mutation(in_shift_s=0.030),
    "beat +60 ms": Mutation(in_shift_s=0.060),
    "one beat late": Mutation(in_shift_s=BEAT_S),
    "second kick/bass in overlap": Mutation(in_low_unheld=True),
    "two vocals overlap": Mutation(vocals_overlap=True),
    "3 LU loudness bump": Mutation(bump_lu=3.0),
    "loop-seam click": Mutation(seam_click=True),
    "clipping": Mutation(clip_gain=4.0),
    "swap 26 ms late": Mutation(step_late_s=0.026),
    "1 s dropout": Mutation(gap_s=1.0),
    "6 LU under the outgoing natural level for 4 bars": Mutation(dip_lu=6.0, dip_bars=4.0),
    "outgoing mids thin 3 LU": Mutation(out_mids_thin_lu=3.0),
}


@pytest.mark.parametrize("name", list(MUTATIONS))
def test_mutation_drops_m_beyond_the_noise_band(name: str) -> None:
    clean, band = _clean(), _noise_band()
    mutated = _score(MUTATIONS[name])
    after = 0.0 if mutated.score is None else mutated.score
    print(f"{name}: M {clean:.4f} -> {after:.4f} (band {band:.4f}) vetoes={[v.name for v in mutated.vetoes]}")
    assert mutated.score is not None, (
        f"if {name} leaves M unscored then the mutation hid behind unknown: {mutated.unknown}"
    )
    assert clean - after > max(2 * band, MIN_DROP), f"if {name} does not drop M then M cannot see it"


def test_beat_shift_30ms_reads_30ms() -> None:
    metrics = _score(MUTATIONS["beat +30 ms"]).metrics
    assert abs(metrics["beat_offset_ms_p50"] - 30.0) <= 3.0, metrics


def test_beat_shift_60ms_fires_the_beat_offset_veto() -> None:
    result = _score(MUTATIONS["beat +60 ms"])
    assert result.score == 0.0
    assert [v.name for v in result.vetoes] == ["beat_offset_p95"]
    assert result.vetoes[0].threshold == BEAT_OFFSET_P95_VETO_MS


def test_one_beat_late_fires_bar_lag_while_beat_offset_reads_aligned() -> None:
    """Spec 10 trap 4: on the beat, off the downbeat. Folding mod one beat alone would read this as perfect."""
    result = _score(MUTATIONS["one beat late"])
    assert result.metrics["bar_lag_beats"] == 1.0
    assert result.metrics["beat_offset_ms_p95"] < ALIGNMENT_KNEE_MS
    assert [v.name for v in result.vetoes] == ["bar_lag"]
    assert result.score == 0.0


def test_clipping_is_reported_and_never_vetoed() -> None:
    result = _score(MUTATIONS["clipping"])
    assert "true_peak" not in [v.name for v in result.vetoes], "if clipping vetoes then peaks are gating again"
    assert result.metrics["true_peak_dbtp"] > -1.0 and result.metrics["true_peak_would_veto"] is True


def test_each_layer1_mutation_moves_its_own_factor() -> None:
    """Both directions: the targeted factor falls, and the clean value of the others is not what moved M."""
    clean = _score().factors
    targets = {
        "second kick/bass in overlap": "bass_overlap",
        "two vocals overlap": "vocal_overlap",
        "3 LU loudness bump": "loudness_continuity",
        "loop-seam click": "seam_clicks",
        "swap 26 ms late": "cut_to_downbeat",
        "beat +15 ms": "alignment",
    }
    for name, factor in targets.items():
        mutated = _score(MUTATIONS[name]).factors
        assert mutated[factor] < clean[factor] - 0.2, (
            f"if {name} leaves {factor} at {mutated[factor]} then that factor is blind"
        )


# ----- unknown is never a pass ----------------------------------------------------------


def test_missing_vocal_stem_reads_unknown_with_its_reason_never_zero() -> None:
    mix, out_deck, in_deck, ctx = render()
    bare = RenderContext(
        sample_rate=ctx.sample_rate,
        beat_s=ctx.beat_s,
        overlap_s=ctx.overlap_s,
        out_downbeats_s=ctx.out_downbeats_s,
        tempo_ratios=ctx.tempo_ratios,
        intent=ctx.intent,
        shape=ctx.shape,
        steps=ctx.steps,
    )
    result = score_render(mix, out_deck, in_deck, bare)
    assert "vocal_overlap" not in result.factors and "vocal_overlap_s" not in result.metrics
    assert "no vocal stem" in result.unknown["vocal_overlap"]
    assert result.measured.k == 6


def test_below_four_core_factors_there_is_no_composite() -> None:
    """A clean render described too thinly to measure (no grid, no steps, no stems) gets no score, not a guess."""
    mix, out_deck, in_deck, ctx = render()
    thin = RenderContext(
        sample_rate=ctx.sample_rate,
        beat_s=ctx.beat_s,
        overlap_s=ctx.overlap_s,
        out_downbeats_s=(0.0,),
        tempo_ratios=(1.0,),
        intent=ctx.intent,
        shape=ctx.shape,
    )
    result = score_render(mix, out_deck, in_deck, thin)
    print(result.measured, result.unknown)
    assert result.vetoes == ()
    assert result.measured.k < 4
    assert result.score is None and "4" in result.unknown["score"]


def test_a_veto_outranks_unknown() -> None:
    """With the incoming deck silent the dropout veto names M = 0, even though most factors are unmeasured."""
    _, out_deck, in_deck, ctx = render()
    result = score_render(out_deck, out_deck, 0.0 * in_deck, ctx)
    assert "dropout" in [v.name for v in result.vetoes]
    assert result.score == 0.0


# ----- review round 1 (PR #7597): every veto the ADR claims is shown able to fire, and not to overshoot ----


def _with(**changes: Any) -> ScoreResult:
    mix, out_deck, in_deck, ctx = render()
    return score_render(mix, out_deck, in_deck, dataclasses.replace(ctx, **changes))


def test_dropout_fires_on_a_one_second_hole_and_not_on_a_045_s_one() -> None:
    assert "dropout" in [v.name for v in _score(MUTATIONS["1 s dropout"]).vetoes]
    short = _score(Mutation(gap_s=0.45))
    assert "dropout" not in [v.name for v in short.vetoes], (
        f"if a 0.45 s hole vetoes then the 0.5 s bound overshoots: {short.metrics}"
    )


def test_stretch_veto_fires_past_8_percent_and_not_inside_it() -> None:
    fired = _with(tempo_ratios=(1.0, 1.09), pitch_follows_tempo=False)
    assert [v.name for v in fired.vetoes] == ["stretch"] and fired.score == 0.0
    inside = _with(tempo_ratios=(1.0, 1.07), pitch_follows_tempo=False)
    assert inside.vetoes == () and inside.metrics["stretch_pct"] == pytest.approx(7.0)


def test_key_shift_veto_fires_past_2_semitones_and_reads_unknown_under_master_tempo() -> None:
    fired = _with(tempo_ratios=(1.0, 1.13))  # 2.12 semitones; also past the 8 % stretch
    assert "key_shift" in [v.name for v in fired.vetoes]
    inside = _with(tempo_ratios=(1.0, 1.06))  # 1.01 semitones
    assert "key_shift" not in [v.name for v in inside.vetoes]
    kept_pitch = _with(tempo_ratios=(1.0, 1.06), pitch_follows_tempo=False)
    assert "key_shift" in kept_pitch.unknown and "key_shift_st" not in kept_pitch.metrics


@pytest.mark.parametrize(
    ("parity", "fires"),
    [
        ((6.0, 0.1), ["parity_beat_ms"]),
        ((1.0, 0.6), ["parity_lu"]),
        ((-6.0, -0.6), ["parity_beat_ms", "parity_lu"]),
        ((4.9, 0.49), []),
    ],
)
def test_parity_veto_fires_past_5_ms_or_half_an_lu(parity: tuple[float, float], fires: list[str]) -> None:
    result = _with(parity=parity)
    assert [v.name for v in result.vetoes] == fires
    assert "parity" not in result.unknown


# ----- round 1 (OLTF #11 -> #20): an outgoing rolling bass must not be read as the beat -------------


def test_outgoing_rolling_bass_louder_than_its_kick_still_reads_aligned() -> None:
    """Round 1 root cause: the template carried the outgoing bass stabs a sixteenth after each kick,
    the correlation locked onto them, and aligned beats read 110 to 235 ms off."""
    result = _score(Mutation(out_rolling_bass=True))
    print(result.metrics)
    assert "beat_offset_p95" not in [v.name for v in result.vetoes]
    # Round 6: stabs louder than the kick make the kick non-dominant, so alignment may read unknown;
    # what it may never do is read the stabs' 120 ms as an offset.
    if "alignment" not in result.unknown:
        assert result.metrics["beat_offset_ms_p95"] < BEAT_OFFSET_P95_VETO_MS
        assert abs(result.metrics["beat_offset_ms_p50"]) < 3.0


def test_outgoing_rolling_bass_does_not_hide_a_real_shift() -> None:
    """Both directions: with the same stabs, a real +30 ms shift of the incoming deck still vetoes."""
    result = _score(Mutation(out_rolling_bass=True, in_shift_s=0.030))
    # Round 6: either the shift is measured and vetoes, or alignment is unknown: never a pass.
    assert "beat_offset_p95" in [v.name for v in result.vetoes] or "alignment" in result.unknown, result.metrics
    assert "alignment" not in result.factors or result.factors["alignment"] < 0.2


def test_too_few_timeable_beats_is_unknown_never_a_pass() -> None:
    """With almost every beat ambiguous, alignment is unknown, not scored from the few left."""
    import apps.transition_score.scorer as scorer_module

    saved = scorer_module.ONSET_DOMINANCE
    scorer_module.ONSET_DOMINANCE = 1e9  # nothing is dominant
    try:
        result = score_render(*render())
    finally:
        scorer_module.ONSET_DOMINANCE = saved
    assert "alignment" in result.unknown and "alignment" not in result.factors
    assert "beat_offset_p95" not in [v.name for v in result.vetoes]


# ----- round 2: a cut is held to the downbeat that sounds, not to the time the plan asked for ----------


def test_a_faithfully_rendered_off_bar_cut_still_scores_off_the_bar() -> None:
    clean = _score()
    off_bar = _score(Mutation(step_late_s=BEAT_S / 4, plan_says_late=True))
    print(off_bar.metrics.get("cut_error_ms_max"), off_bar.factors.get("cut_to_downbeat"))
    assert off_bar.metrics["cut_error_ms_max"] > 100.0, (
        "if a cut the plan put off the bar reads on time then the grid is the plan"
    )
    assert off_bar.factors["cut_to_downbeat"] < clean.factors["cut_to_downbeat"] - 0.5
    assert clean.metrics["cut_error_ms_max"] < CUT_KNEE_MS, "the clean swap on the bar must still read on time"


def test_cut_timing_is_unknown_when_no_downbeat_can_be_read() -> None:
    mix, out_deck, in_deck, ctx = render()
    result = score_render(mix, out_deck, in_deck, dataclasses.replace(ctx, out_downbeats_s=()))
    assert "cut_to_downbeat" in result.unknown and "cut_to_downbeat" not in result.factors


# ----- review round 4: every veto threshold has a pinned inside/outside pair -----------------------------------


def _veto_names(result: ScoreResult) -> list[str]:
    return [v.name for v in result.vetoes]


def test_beat_offset_veto_pair_15_ms_inside_30_ms_outside() -> None:
    inside, outside = _score(MUTATIONS["beat +15 ms"]), _score(MUTATIONS["beat +30 ms"])
    assert inside.metrics["beat_offset_ms_p95"] < BEAT_OFFSET_P95_VETO_MS < outside.metrics["beat_offset_ms_p95"]
    assert "beat_offset_p95" not in _veto_names(inside)
    assert "beat_offset_p95" in _veto_names(outside)


def test_bar_lag_veto_pair_aligned_inside_one_beat_outside() -> None:
    assert _score().metrics["bar_lag_beats"] == 0.0 and "bar_lag" not in _veto_names(_score())
    assert "bar_lag" in _veto_names(_score(MUTATIONS["one beat late"]))


@pytest.mark.parametrize(("peak_dbtp", "fires"), [(-1.3, False), (-0.7, True)])
def test_true_peak_would_veto_pair_around_minus_1_dbtp(peak_dbtp: float, fires: bool) -> None:
    """The overlap is scaled so its true peak lands just inside or just outside -1 dBTP (the solos stay quieter)."""
    mix, out_deck, in_deck, ctx = render()
    start, end = (round(x * ctx.sample_rate) for x in ctx.overlap_s)
    from apps.transition_score.signals import true_peak_dbtp

    gain = 10 ** ((peak_dbtp - true_peak_dbtp(mix[start:end])) / 20)
    for deck in (mix, out_deck, in_deck):
        deck[start:end] *= gain
    result = score_render(mix, out_deck, in_deck, ctx)
    assert abs(result.metrics["true_peak_dbtp"] - peak_dbtp) < 0.05
    assert result.metrics["true_peak_would_veto"] is fires
    assert "true_peak" not in _veto_names(result)


@pytest.mark.parametrize(("ratio", "fires"), [(2 ** (1.9 / 12), False), (2 ** (2.1 / 12), True)])
def test_key_shift_veto_pair_around_2_semitones(ratio: float, fires: bool) -> None:
    result = _with(tempo_ratios=(1.0, ratio))
    assert ("key_shift" in _veto_names(result)) is fires


@pytest.mark.parametrize(("ratio", "fires"), [(1.079, False), (1.081, True)])
def test_stretch_veto_pair_around_8_percent(ratio: float, fires: bool) -> None:
    result = _with(tempo_ratios=(1.0, ratio), pitch_follows_tempo=False)
    assert ("stretch" in _veto_names(result)) is fires


def test_no_tempo_telemetry_leaves_stretch_and_key_shift_unknown_never_passed() -> None:
    result = _with(tempo_ratios=())
    assert "stretch" in result.unknown and "key_shift" in result.unknown
    assert "stretch_pct" not in result.metrics and "key_shift_st" not in result.metrics


# ----- round 6: a bass-led outgoing section reads unknown or aligned, never a phantom offset ----------------


def test_bass_led_outgoing_section_never_reads_a_phantom_offset() -> None:
    """Round 6 (OLTF #11 -> #20 bar 61): a bass hit 45 ms before each grid beat, louder than the kick."""
    result = _score(Mutation(out_bass_led=True))
    print(result.metrics.get("alignment_quarters"), result.unknown.get("alignment"))
    assert "beat_offset_p95" not in _veto_names(result), (
        "if a bass-led section vetoes as misaligned then the measure invents offsets"
    )
    if "alignment" not in result.unknown:
        assert (
            abs(result.metrics["beat_offset_ms_p50"]) < 3.0
            and result.metrics["beat_offset_ms_p95"] < BEAT_OFFSET_P95_VETO_MS
        )


def test_kick_led_section_still_vetoes_a_real_30_ms_shift() -> None:
    result = _score(MUTATIONS["beat +30 ms"])
    assert "alignment" not in result.unknown, (
        "if the round 6 gate makes a clean kick-led overlap unknown, it hides real shifts"
    )
    assert all(q["timed"] >= 8 for q in result.metrics["alignment_quarters"])
    assert "beat_offset_p95" in _veto_names(result)


def test_alignment_is_reported_per_overlap_quarter() -> None:
    quarters = _score().metrics["alignment_quarters"]
    assert len(quarters) == 4 and sum(q["timed"] for q in quarters) == _score().metrics["beats_measured"]


# ----- round 7: coverage rule, both ways ---------------------------------------------------------------


def _quarters(counts: list[int]) -> list[dict[str, object]]:
    return [{"timed": c, "p50_ms": None, "p95_ms": None} for c in counts]


@pytest.mark.parametrize(
    ("counts", "measured"),
    [
        ([1, 0, 1, 1], False),  # bar-61-like: 3 timed, nothing to measure
        ([0, 1, 1, 0], False),
        ([6, 11, 4, 5], True),  # bar-95-like (round 6 counts): 26 timed over every quarter
        ([8, 8, 8, 8], True),  # the 8-bar overlap: 8 beats per quarter
        ([16, 0, 0, 0], False),  # bunched in one quarter: not the overlap
        ([7, 7, 1, 0], False),  # 15 timed: under 16
        ([8, 8, 0, 0], False),  # 2 covered quarters only
        ([6, 6, 2, 2], True),  # exactly the floor
    ],
)
def test_alignment_coverage_rule(counts: list[int], measured: bool) -> None:
    from apps.transition_score.scorer import alignment_coverage_gap

    gap = alignment_coverage_gap(_quarters(counts))
    assert (gap is None) is measured, gap


def test_a_steady_minus_7_ms_bass_heavy_exit_is_measured_and_reads_its_p95_honestly() -> None:
    """Bar-95-like: 4 to 11 timed beats per quarter at a constant -7 ms (plus the detector's jitter)."""
    rng = np.random.default_rng(3)
    mix, out_deck, in_deck, ctx = render()
    start, end = ctx.overlap_s
    span = (end - start) / 4
    timed = [
        (start + i * span + k * 0.01, -7.0 + float(rng.normal(0, 3)), "low")
        for i, n in enumerate((6, 11, 4, 5))
        for k in range(n)
    ]
    from apps.transition_score import scorer as scorer_module

    saved = scorer_module.beat_offsets_ms
    scorer_module.beat_offsets_ms = lambda *a, **k: (
        timed,
        60,
    )  # the timing stage stands in; the rule under test is downstream
    try:
        result = score_render(mix, out_deck, in_deck, ctx)
    finally:
        scorer_module.beat_offsets_ms = saved
    values = np.array([ms for _, ms, _ in timed])
    assert "alignment" not in result.unknown
    assert (
        abs(result.metrics["beat_offset_ms_p50"] - float(np.median(values))) < 0.01
        and abs(result.metrics["beat_offset_ms_p50"] + 7) < 2.5
    )
    assert result.metrics["beat_offset_ms_p95"] == pytest.approx(float(np.percentile(np.abs(values), 95)), abs=0.01)


# ----- round 10 (F3): beats whose kick band is killed are timed on the 2-8 kHz band ------------------------


def test_killed_incoming_lows_plus_30_ms_now_vetoes_on_the_high_band() -> None:
    """At 0.4.0 this render scored M 0.883 (alignment unknown, no veto): a bass swap hid a 30 ms flam."""
    result = _score(Mutation(in_lows_killed=True, in_shift_s=0.030))
    print(result.metrics.get("beats_low"), result.metrics.get("beats_high"), result.metrics.get("beat_offset_ms_p50"))
    assert result.metrics["beats_high"] > 0
    assert "beat_offset_p95" in _veto_names(result) and result.score == 0.0
    assert abs(result.metrics["beat_offset_ms_p50"] - 30.0) <= 3.0


def test_killed_incoming_lows_aligned_is_measured_and_clean() -> None:
    result = _score(Mutation(in_lows_killed=True))
    assert "alignment" not in result.unknown and result.metrics["beats_high"] > 0
    assert result.metrics["beat_offset_ms_p95"] < ALIGNMENT_KNEE_MS and not result.vetoes


def test_beats_with_both_lows_present_stay_on_the_kick_band() -> None:
    """The clean render times the same 51 beats on the low band as at 0.4.0; the high band only adds the
    early-overlap beats where the incoming fader is still near zero (its lows absent)."""
    metrics = _score().metrics
    assert metrics["beats_low"] == 51
    assert metrics["beats_high"] + metrics["beats_low"] == metrics["beats_measured"]


def test_high_band_reach_stays_under_half_the_hat_period() -> None:
    """Round 10: with a half-beat reach the smoke pair's hats aliased to a 234 ms (half-beat) offset."""
    from apps.transition_score.scorer import HIGH_BAND_LAG_BEATS

    assert 0.03 * 128 / 60 < HIGH_BAND_LAG_BEATS < 0.25


def test_an_all_silent_render_returns_its_dropout_veto_not_a_division_error() -> None:
    """[if] a seam's neighboring beats are silent [then] scoring returns the dropout veto instead of dividing by
    zero; a click against silence still reads as a click, else stop (Codex/Sol P2 on #7597)."""
    mix, _, _, ctx = render()
    silent = np.zeros_like(mix)
    result = score_render(silent, silent, silent, ctx)
    assert "dropout" in [v.name for v in result.vetoes]
    assert result.metrics["seam_clicks"] == 0
    clicked = silent.copy()
    clicked[int(ctx.steps[0].at_s * ctx.sample_rate) :][:5] = 0.5
    assert score_render(clicked, silent, silent, ctx).metrics["seam_clicks"] == 1


# ----- round 14: loudness loss is measured as a near-hygiene fault (the maintainer, oltf-r1 Thu 29 Oct 2026) ------------------
# Loss is read against what the outgoing would be playing on its own (``out_natural``), never a line between the solo
# levels; its 3 LU / 2-bar line is mimicked (scorer.LOUDNESS_LOSS_VETO_LU) and the veto ships OFF, so these tests
# switch it on to show it can fire. Exempt only on an explicit plan
# field: intent wind_down, or shape lull_then_drop.


@pytest.fixture
def veto_on(monkeypatch: pytest.MonkeyPatch) -> None:
    import apps.transition_score.scorer as scorer_module

    monkeypatch.setattr(scorer_module, "LOUDNESS_LOSS_VETO_ON", True)


def _hole(
    intent: str | None = "smooth", shape: str | None = None, dip_lu: float = 6.0, dip_bars: float = 4.0
) -> ScoreResult:
    mix, out_deck, in_deck, ctx = render(Mutation(dip_lu=dip_lu, dip_bars=dip_bars))
    return score_render(mix, out_deck, in_deck, dataclasses.replace(ctx, intent=intent, shape=shape))


def test_the_veto_is_off_by_default_but_its_measurement_is_reported() -> None:
    """[if] the mix sits 6 LU under the outgoing's natural level for 4 bars with the veto off [then] M is not 0, and
    the would-veto flag and the bars it measured are in the metrics for the review page, [else stop]."""
    result = _hole()
    assert LOUDNESS_LOSS_VETO_ON is False
    assert result.vetoes == () and result.score is not None and result.score > 0.0
    assert result.metrics["loudness_loss_would_veto"] is True and result.metrics["loudness_loss_bars"] > 2.0


@pytest.mark.usefixtures("veto_on")
def test_a_6_lu_hole_under_the_outgoing_natural_level_for_4_bars_vetoes_with_its_numbers() -> None:
    result = _hole()
    print(result.metrics["loudness_loss_bars"], result.vetoes)
    assert [v.name for v in result.vetoes] == ["loudness_loss"] and result.score == 0.0
    assert result.vetoes[0].threshold == LOUDNESS_LOSS_VETO_BARS == 2.0
    assert result.vetoes[0].value > 2.3, "if a 4-bar hole reads near the limit then the run is mismeasured"


@pytest.mark.usefixtures("veto_on")
def test_a_6_lu_hole_for_2_bars_is_measured_but_does_not_veto() -> None:
    """The inside control: a hole well past 3 LU that lasts 2 bars stays a matter of degree."""
    result = _hole(dip_lu=6.0, dip_bars=2.0)
    assert 0.5 < result.metrics["loudness_loss_bars"] < LOUDNESS_LOSS_VETO_BARS, result.metrics
    assert result.vetoes == () and result.score is not None and result.score > 0.0


@pytest.mark.usefixtures("veto_on")
def test_a_breakdown_in_the_outgoing_natural_level_is_never_transition_loss() -> None:
    """[if] the outgoing track itself drops 10 LU for 4 bars (its breakdown) and the mix follows it [then] no loss is
    read, though the old solo-to-solo line would read a 10 LU dip there, [else stop] (the maintainer: a lull in the outgoing's
    own structure is never transition-caused loss)."""
    result = _score(Mutation(out_breakdown_lu=10.0))
    assert result.metrics["loudness_dip_lu"] > 8.0, "control: the line-based dip must see the breakdown"
    assert result.metrics["loudness_loss_bars"] == 0.0 and result.vetoes == ()


@pytest.mark.usefixtures("veto_on")
def test_the_clean_render_reads_no_loudness_loss() -> None:
    result = score_render(*render())
    assert result.metrics["loudness_loss_bars"] == 0.0 and result.metrics["intent"] == "smooth"
    assert result.vetoes == ()


@pytest.mark.usefixtures("veto_on")
def test_wind_down_intent_skips_the_veto_but_not_the_factor() -> None:
    """[if] the plan explicitly asks to wind down [then] the hole is no veto, the exemption is recorded and the
    loudness factor still pulls M down, [else stop]."""
    exempt = _hole(intent="wind_down")
    assert exempt.vetoes == () and "wind_down" in exempt.metrics["loudness_loss_exempt"]
    assert exempt.score is not None and exempt.score < _clean() - 0.1


@pytest.mark.usefixtures("veto_on")
def test_lull_then_drop_shape_skips_the_veto_on_any_intent() -> None:
    """[if] the plan's shape is lull_then_drop (the outgoing's lull held until the incoming drop lands) [then] the
    hole is no veto and the exemption names the shape, [else stop]."""
    exempt = _hole(intent=None, shape="lull_then_drop")
    assert exempt.vetoes == () and "lull_then_drop" in exempt.metrics["loudness_loss_exempt"]


@pytest.mark.usefixtures("veto_on")
@pytest.mark.parametrize("intent", [None, "smooth", "energy_up", "showy", "vocals_clean"])
@pytest.mark.parametrize("shape", [None, "blend"])
def test_without_wind_down_or_lull_then_drop_the_veto_applies(intent: str | None, shape: str | None) -> None:
    """The overshoot control: no stated intent, any other intent, no shape or another shape still vetoes the hole."""
    assert [v.name for v in _hole(intent=intent, shape=shape).vetoes] == ["loudness_loss"], (intent, shape)


def test_without_the_outgoing_natural_audio_loudness_loss_is_unknown_never_a_pass() -> None:
    mix, out_deck, in_deck, ctx = render(Mutation(dip_lu=6.0, dip_bars=4.0))
    result = score_render(mix, out_deck, in_deck, dataclasses.replace(ctx, out_natural=None))
    assert "outgoing source" in result.unknown["loudness_loss"]
    assert "loudness_loss_bars" not in result.metrics and "loudness_loss_would_veto" not in result.metrics


def test_recovery_after_the_bass_hit_is_reported_and_its_absence_is_unknown() -> None:
    """[if] the plan names the incoming bass hit [then] the gap to the outgoing's level in the bar after it is
    reported (the clean blend is back at once; a hole still open at the hit is not), [else] it is unknown, never a
    pass, [else stop]."""
    from tests.transition_score.synth import SWAP_S

    mix, out_deck, in_deck, ctx = render()
    clean = score_render(mix, out_deck, in_deck, dataclasses.replace(ctx, bass_hit_s=SWAP_S))
    assert clean.metrics["loudness_recovered"] is True, clean.metrics
    mix, out_deck, in_deck, ctx = render(Mutation(dip_lu=6.0, dip_bars=4.0))
    held = score_render(mix, out_deck, in_deck, dataclasses.replace(ctx, bass_hit_s=SWAP_S - BEAT_S))
    assert held.metrics["loudness_recovered"] is False and held.metrics["loudness_recovery_gap_lu"] > 1.0
    assert "bass entry" in _score().unknown["loudness_recovery"]


def test_an_unknown_intent_is_refused_never_read_as_one() -> None:
    _, _, _, ctx = render()
    with pytest.raises(ValueError, match="intent"):
        dataclasses.replace(ctx, intent="chill")


def test_loudness_loss_doubles_the_loudness_weight() -> None:
    from apps.transition_score.scorer import PRIOR_WEIGHTS

    assert PRIOR_WEIGHTS["loudness_continuity"] == 0.4


# ----- round 15: the low-band handoff and the true-peak reference ---------------------------------------------------


def test_maintainer_low_handoff_is_not_two_basslines_and_keeps_the_low_end_steady() -> None:
    """[if] the incoming LO is parked at -8.7 dB and crossed over 4 bars (the maintainer's rule) [then] no beat has both lows
    near full and the low end stays within the knee, while the old 20 dB reading called most bars a clash, [else stop]."""
    m = _score(Mutation(maintainer_low_handoff=True))
    assert m.metrics["bass_both_full_beats"] == 0 and m.metrics["bass_overlap_bars_v0"] > 4
    assert m.factors["low_continuity"] > 0.5


def test_a_low_pile_up_counts_both_full_beats_and_a_low_hole_reads_as_a_hole() -> None:
    pile, hole = _score(Mutation(in_low_unheld=True)), _score(Mutation(low_hole=True))
    assert pile.metrics["bass_both_full_beats"] >= 8 and pile.factors["bass_overlap"] < 0.1
    assert hole.metrics["low_hole_db"] > 20 and hole.factors["low_continuity"] < 0.05


@pytest.mark.requirement("TRYER-01")
def test_a_bass_free_solo_reports_bass_overlap_unknown() -> None:
    """[if] a solo has no measurable bass [then] bass_overlap is unknown, never a pile-up, [else stop]."""
    out_gone = _score(Mutation(out_solo_low_killed=True))
    in_gone = _score(Mutation(in_solo_low_killed=True))
    both = _score(Mutation(out_solo_low_killed=True, in_solo_low_killed=True))
    for result, token in (
        (out_gone, "outgoing"),
        (in_gone, "incoming"),
        (both, "outgoing"),
    ):
        assert "bass_overlap" in result.unknown and token in result.unknown["bass_overlap"]
        assert "bass_overlap" not in result.factors
        assert "bass_both_full_beats" not in result.metrics
        assert result.measured.k == 6 and result.measured.n == 7
        assert result.scorer_version == SCORER_VERSION == "0.10.0"
        assert result.score is not None
    pile = _score(Mutation(in_low_unheld=True))
    assert "bass_overlap" in pile.factors and pile.metrics["bass_both_full_beats"] >= 8


def test_an_outgoing_peaking_alone_in_the_overlap_is_not_a_transition_over() -> None:
    """[if] only the outgoing plays in the overlap and peaks over -1 dBTP [then] the reading stays clear (the transition
    added nothing over the louder deck), [else stop]."""
    _, out_deck, in_deck, ctx = render()
    hot = out_deck * 3.0
    result = score_render(hot, hot, 0.0 * in_deck, ctx)
    assert result.metrics["true_peak_dbtp"] > -1.0 and result.metrics["true_peak_would_veto"] is False


def test_early_handover_reads_the_bars_before_the_incoming_bass_exists_and_exempts_a_lull() -> None:
    """[if] the incoming's own bass enters 4 bars after its low takes over [then] early_handover reads 4 bars and its
    factor is low; at the switch it is 0; a lull_then_drop plan is exempt, [else stop]."""
    from tests.transition_score.synth import BAR_S, SWAP_S

    mix, out_deck, in_deck, ctx = render()
    late = score_render(mix, out_deck, in_deck, dataclasses.replace(ctx, in_bass_entry_s=SWAP_S + 4 * BAR_S))
    switch = late.metrics["low_switch_s"]
    assert late.metrics["early_handover_bars"] > 3.5 and late.factors["early_handover"] < 0.1, late.metrics
    on_time = score_render(mix, out_deck, in_deck, dataclasses.replace(ctx, in_bass_entry_s=switch))
    assert on_time.metrics["early_handover_bars"] == 0.0 and on_time.factors["early_handover"] == 1.0
    lull = score_render(
        mix, out_deck, in_deck, dataclasses.replace(ctx, in_bass_entry_s=SWAP_S + 4 * BAR_S, shape="lull_then_drop")
    )
    assert "early_handover" not in lull.factors and "lull_then_drop" in lull.metrics["early_handover_exempt"]
    assert "early_handover" in _score().unknown


def test_low_hole_switch_waits_for_a_present_incoming_and_falls_back_to_the_hit() -> None:
    """0.7.1: [if] the incoming is louder than the outgoing but parked (-8.7 dB under its solo) [then] it does not take
    the reference, the plan's bass hit does; [if] it is present (-4 dB) [then] it switches there, [else stop]."""
    from apps.transition_score.scorer import _content_switch

    beats = [((float(i), float(i) + 0.5), i % 4) for i in range(8)]
    ctx = dataclasses.replace(render()[3], bass_hit_s=5.0)
    raw_out, raw_in = np.full(8, -40.0), np.full(8, -30.0)
    assert _content_switch(np.full(8, -8.7), raw_out, raw_in, beats, ctx) == 5
    assert _content_switch(np.full(8, -4.0), raw_out, raw_in, beats, ctx) == 0


# ----- #10187: three-band continuity against the outgoing at unity -----------------------------------------


def test_three_band_split_puts_each_tone_in_its_engine_band() -> None:
    sr = 48000
    length = sr
    t = np.arange(length) / sr

    def band_power(hz: float) -> dict[str, float]:
        tone = np.sin(2 * np.pi * hz * t).astype(np.float64)
        bands = signals.three_band(tone, sr)
        return {name: float(np.mean(bands[name] ** 2)) for name in signals.ENGINE_BANDS}

    for hz, own in ((100.0, "low"), (1000.0, "mid"), (10000.0, "high")):
        power = band_power(hz)
        for name, value in power.items():
            if name == own:
                assert value > 0.0
            else:
                assert value < power[own] / 100.0, (hz, name, value, power[own])


@cache
def _loudness_continuity_noise_band() -> float:
    values = [_score(Mutation(), seed).factors["loudness_continuity"] for seed in NOISE_SEEDS]
    return max(values) - min(values)


@pytest.mark.requirement("TRANSAI-10187")
def test_mids_thinning_3lu_drops_band_mid_below_the_knee_while_loudness_continuity_holds() -> None:
    """[if] the outgoing mids thin 3 LU in the mix only [then] band_continuity_mid < 0.5, [else stop]."""
    clean_lc = _score().factors["loudness_continuity"]
    mutated = _score(Mutation(out_mids_thin_lu=3.0))
    lc_band = _loudness_continuity_noise_band()
    assert mutated.factors["band_continuity_mid"] < 0.5
    assert mutated.metrics["band_deficit_lu_mid"] > LOUDNESS_KNEE_LU
    assert mutated.factors["band_continuity_low"] > 0.9 and mutated.factors["band_continuity_high"] > 0.9
    assert abs(clean_lc - mutated.factors["loudness_continuity"]) < max(2 * lc_band, MIN_DROP)


def test_an_outgoing_breakdown_is_the_track_not_band_loss() -> None:
    result = _score(Mutation(out_breakdown_lu=6.0))
    for name in BAND_FACTORS:
        assert result.factors[name] >= 0.95


def test_without_the_outgoing_at_unity_the_band_factors_are_unknown_naming_the_file_never_zero() -> None:
    mix, out_deck, in_deck, ctx = render()
    result = score_render(mix, out_deck, in_deck, dataclasses.replace(ctx, out_natural=None))
    for name in BAND_FACTORS:
        assert name not in result.factors
        reason = result.unknown[name]
        assert "source" in reason and "decks/deck" in reason
    assert result.score is not None


def test_band_window_closes_at_the_handover_and_is_unknown_without_one() -> None:
    from tests.transition_score.synth import BAR_S, SWAP_S

    mix, out_deck, in_deck, ctx = render()
    hit = SWAP_S - 4 * BAR_S
    with_hit = score_render(mix, out_deck, in_deck, dataclasses.replace(ctx, bass_hit_s=hit))
    assert with_hit.metrics["band_handover"] == "bass_hit"
    assert with_hit.metrics["band_window_s"][1] == round(hit, 3)
    no_step = score_render(mix, out_deck, in_deck, dataclasses.replace(ctx, steps=()))
    for name in BAND_FACTORS:
        assert name in no_step.unknown and "handover" in no_step.unknown[name]


def test_band_continuity_weighs_a_tenth_each_and_is_not_core() -> None:
    for name in BAND_FACTORS:
        assert PRIOR_WEIGHTS[name] == 0.1
        assert name not in CORE_FACTORS
