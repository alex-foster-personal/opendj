"""Amendment 7 and its refinement: alignment must refuse before it guesses.

The three probes are the shapes lane A's adversarial verification used to break
raw-waveform correlation -- a pure sine, a transient-free crescendo, and
independent noise. Raw correlation returned lag 100 for a true lag of 300 at
corr 0.9995, and -101 at 0.9996 on the crescendo: wrong answers at maximal
reported confidence, on exactly the F2/F6 material classes.

The outcomes asserted here are PINNED against lane A's reference implementation
(7068f21f alignment + onset + CLI, 4250c732 gates enforced): sine RAISES,
crescendo RAISES, noise RAISES, and the positive controls come back exact. A
guard stack that refuses everything is not an instrument, which is why the
controls sit in the same file as the refusals.

One-line contract per test, in the house "if X does/doesn't Y then broken"
form, is carried in each test's docstring.
"""

from __future__ import annotations

import numpy as np
import pytest

from scripts.quality import metrics, probes

SAMPLE_RATE = metrics.SAMPLE_RATE_HZ

# ----- amendment 7: alignment on the envelope-rise carrier -------------------


@pytest.mark.parametrize("true_delay", [0, 64, 300, 1234, 5292])
def test_known_delay_is_recovered_on_transient_rich_material(true_delay: int) -> None:
    """If a known integer delay is not recovered within 1 ms then alignment is broken."""
    reference = probes.percussive(4.0)
    rendered = probes.delay(reference, true_delay)
    alignment = metrics.estimate_delay_samples(reference, rendered, label="synthetic")
    assert abs(alignment.delay_samples - true_delay) <= metrics.ALIGN_FINE_HOP_SAMPLES * 2, (
        f"expected ~{true_delay}, got {alignment.delay_samples} at corr {alignment.correlation:.4f}"
    )
    assert alignment.correlation > metrics.ALIGN_CORRELATION_FLOOR
    assert alignment.carrier == "half-wave-rectified envelope rise"


def test_alignment_carrier_is_never_the_raw_waveform() -> None:
    """If alignment reports any carrier but envelope rise then amendment 7 is violated."""
    reference = probes.percussive(2.0)
    alignment = metrics.estimate_delay_samples(reference, probes.delay(reference, 128))
    assert "envelope rise" in alignment.carrier
    assert "waveform" not in alignment.carrier


def test_pure_sine_raises_rather_than_returning_a_lag() -> None:
    """If a delayed pure sine returns any lag at all then a gate is missing.

    Pinned against lane A's reference impl (7068f21f / 4250c732): sine RAISES.
    Raw-waveform correlation returns lag 100 for a true lag of 300 at corr
    0.9995, and the amendment-7 REFINEMENT records that the carrier fix alone
    does not save it either -- a rectified envelope keeps ~8.5 percent ripple at
    2f and correlation is scale-invariant, so a carrier-only stack still
    cycle-skipped at corr 0.999. Gate (d) is what catches this one.
    """
    reference = probes.pure_sine(1000.0, 2.0)
    with pytest.raises(metrics.ReferenceSelfSimilarityError, match="too periodic"):
        metrics.estimate_delay_samples(reference, probes.delay(reference, 300), label="pure-sine")


def test_transient_free_crescendo_raises_rather_than_returning_a_lag() -> None:
    """If a delayed crescendo returns any lag then a gate is missing.

    Pinned against lane A: crescendo RAISES. Raw-waveform correlation returns
    -101 at corr 0.9996 on this shape.
    """
    reference = probes.crescendo(440.0, 3.0)
    with pytest.raises(metrics.AlignmentError):
        metrics.estimate_delay_samples(reference, probes.delay(reference, 300), label="crescendo")


def test_independent_noise_is_refused_by_the_carrier_correlation_floor() -> None:
    """If two independent noise signals produce a lag then the correlation floor is missing.

    Pinned against lane A: noise RAISES. Independent signals 'align' at corr
    ~0.005 on the carrier and would otherwise be reported as an ordinary row.
    """
    rng = probes.rng()
    left = rng.standard_normal(2 * SAMPLE_RATE)
    right = np.random.default_rng(999).standard_normal(2 * SAMPLE_RATE)
    with pytest.raises(metrics.AlignmentCorrelationFloorError, match="below the floor"):
        metrics.estimate_delay_samples(left, right, label="independent-noise")


@pytest.mark.parametrize("true_delay", [-300, 150])
def test_positive_controls_align_exactly(true_delay: int) -> None:
    """If the two pinned positive controls do not come back exact then alignment is broken.

    Lane A's pinned outcomes for the same gate stack: -300 -> -300 and
    +150 -> +150. A guard stack that refuses everything is not a working
    instrument, so these run beside the three refusals.
    """
    reference = probes.percussive(4.0)
    alignment = metrics.estimate_delay_samples(
        reference, probes.shift(reference, true_delay), label=f"control{true_delay:+d}"
    )
    assert alignment.delay_samples == pytest.approx(
        true_delay, abs=metrics.ALIGN_FINE_HOP_SAMPLES * 2
    ), (
        f"expected {true_delay:+d}, got {alignment.delay_samples:+d} at corr "
        f"{alignment.correlation:.4f}, self-similarity {alignment.self_similarity:.4f}"
    )


def test_carrier_correlation_floor_separates_noise_from_the_worst_real_arm() -> None:
    """If the floor sits outside 0.005..0.148 then it refuses real rows or admits noise.

    Gate (c) is calibrated, not chosen: independent noise reads 0.005 on the
    carrier and the worst real arm reads 0.148. A floor above the latter does
    not make the instrument stricter, it deletes the rows nearest the decision.
    """
    assert 0.005 < metrics.ALIGN_CORRELATION_FLOOR < 0.148
    assert metrics.ALIGN_CORRELATION_FLOOR == 0.05


def test_reference_self_similarity_is_reported_on_every_row() -> None:
    """If a row carries no self-similarity figure then gate (d) cannot be audited."""
    reference = probes.percussive(4.0)
    alignment = metrics.estimate_delay_samples(reference, probes.delay(reference, 128))
    assert 0.0 <= alignment.self_similarity < metrics.ALIGN_SELF_SIMILARITY_CEILING
    assert "self_similarity" in alignment.as_dict()


def _coarse_bound_samples() -> int:
    frames = round(
        metrics.ALIGN_COARSE_MAX_LAG_MS * SAMPLE_RATE / 1000.0 / metrics.ONSET_HOP_SAMPLES
    )
    return frames * metrics.ONSET_HOP_SAMPLES


def test_delay_at_the_coarse_bound_raises_instead_of_clipping() -> None:
    """If a delay at the coarse search edge returns a lag then gate (b) is missing.

    An argmax sitting on the bound means the correlation was still climbing when
    the search ran out, so the reported lag is a clipped guess. Three frames
    inside the bound the same material returns the exact delay, which is what
    makes this a guard rather than a blanket refusal.
    """
    reference = probes.percussive(6.0)
    bound = _coarse_bound_samples()
    with pytest.raises(metrics.AlignmentBoundError, match="coarse-stage argmax"):
        metrics.estimate_delay_samples(reference, probes.delay(reference, bound), label="at-bound")

    inside = bound - metrics.ONSET_HOP_SAMPLES * 3
    alignment = metrics.estimate_delay_samples(
        reference, probes.delay(reference, inside), label="inside"
    )
    assert alignment.delay_samples == pytest.approx(inside, abs=metrics.ALIGN_FINE_HOP_SAMPLES * 2)


@pytest.mark.parametrize("delay_ms", [300, 400])
def test_out_of_window_delay_never_yields_a_confident_row(delay_ms: int) -> None:
    """If a far-out delay comes back at real-arm confidence then the guards are cosmetic.

    Measured while absorbing the refinement, and worth recording because it is
    NOT what I expected: on aperiodic material a delay outside the +-250 ms
    window does not trip the bound at all. The carrier has fully decorrelated by
    then, so the argmax lands wherever the noise peaks, reading 0.047 at a true
    300 ms and 0.039 at 400 ms -- i.e. right ON the 0.05 floor rather than
    safely under it. So the property that actually holds is the weaker one
    asserted here: such a row either refuses, or comes back visibly below the
    0.148 worst-real-arm correlation. Gate (b) is not the thing that catches it.
    """
    reference = probes.aperiodic_bursts()
    try:
        alignment = metrics.estimate_delay_samples(
            reference, probes.delay(reference, int(delay_ms / 1000 * SAMPLE_RATE)), label="far"
        )
    except metrics.AlignmentError:
        return
    assert alignment.correlation < 0.148, (
        f"an out-of-window delay came back at corr {alignment.correlation:.4f}, "
        "indistinguishable from a real arm"
    )


def test_sixteenth_note_alias_is_reported_not_silently_absorbed() -> None:
    """If the one-period alias is not visible in the reported row then broken.

    The spec's trap: on 16th-note material at 103-158 BPM a 120 ms-early render
    aliases onto the next musical period and reports a near-zero lag, without
    tripping a +-25 ms bound. Four of the six fixtures sit in that window. The
    guard here is not a raise -- refusing at this ratio would refuse ordinary
    repetitive dance music -- it is that peak_ratio and the runner-up lag come
    back in the row so the alias is an observable a joint threshold can act on.
    """
    period = int(SAMPLE_RATE * 60.0 / 122.0 / 4.0)  # 5422 samples = 123 ms
    signal = probes.click_train(period)
    alignment = metrics.estimate_delay_samples(
        signal, probes.delay(signal, period), label="16ths-at-122"
    )

    # A periodic signal delayed by exactly one period coincides with itself, so
    # lag 0 is the honest answer and no algorithm can report 5422 here. That is
    # precisely why a bound check cannot prove a trim: this row would sail
    # through +-25 ms while hiding a whole period of displacement.
    assert alignment.delay_samples == pytest.approx(0, abs=64)
    assert abs(alignment.runner_up_delay_samples) == pytest.approx(
        period, abs=metrics.ONSET_HOP_SAMPLES
    ), (
        "the one-period alias must surface as the runner-up lag, otherwise the row "
        f"hides it; read {alignment.runner_up_delay_samples}"
    )
    assert 0.5 < alignment.peak_ratio < metrics.ALIGN_AMBIGUITY_MAX_RATIO, (
        "the alias should be a visible but non-refusing runner-up, ratio read "
        f"{alignment.peak_ratio:.3f}"
    )


def test_indistinguishable_peaks_are_refused() -> None:
    """If two equally-good lags do not raise then the correlation is reporting a guess.

    A click train whose period is an exact whole number of coarse frames
    correlates identically at lag 0 and at one period, so no delay is
    identified and the only honest answer is a refusal. Gate (d) reaches this
    one first -- it inspects the REFERENCE ALONE and so refuses before any
    render is even compared, which is the stronger of the two refusals.
    """
    period = metrics.ONSET_HOP_SAMPLES * 85
    signal = probes.click_train(period)
    with pytest.raises(metrics.ReferenceSelfSimilarityError, match="too periodic"):
        metrics.estimate_delay_samples(signal, probes.delay(signal, period), label="exact-period")


def test_flat_carrier_is_refused_as_degenerate() -> None:
    """If a constant-amplitude signal yields an alignment then the carrier guard is missing."""
    constant = np.ones(SAMPLE_RATE) * 0.5
    with pytest.raises(metrics.DegenerateCarrierError):
        metrics.envelope_rise_carrier(constant, metrics.ONSET_HOP_SAMPLES, label="constant")


def test_apply_delay_lines_the_signals_up() -> None:
    """If applying the measured delay does not restore sample alignment then broken."""
    reference = probes.percussive(2.0)
    rendered = probes.delay(reference, 512)
    alignment = metrics.estimate_delay_samples(reference, rendered)
    left, right = metrics.apply_delay(reference, rendered, 512)
    assert np.allclose(left, right, atol=1e-12)
    assert abs(alignment.delay_samples - 512) <= metrics.ALIGN_FINE_HOP_SAMPLES * 2
