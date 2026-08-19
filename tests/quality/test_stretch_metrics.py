"""Regression tests for the stretch-quality analysis primitives.

One-line contract per test, in the house "if X does/doesn't Y then broken"
form, is carried in each test's docstring.

The alignment probes are the three shapes lane A's 3-agent adversarial
verification used to break raw-waveform correlation (spec amendment 7): a pure
sine, a transient-free crescendo, and independent noise. Raw correlation
returned lag 100 for a true lag of 300 at corr 0.9995, and -101 at 0.9996 on
the crescendo -- wrong answers at maximal reported confidence. The property
asserted here is that this harness never does that: it either returns the true
delay or it refuses.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from scripts.quality import metrics

SAMPLE_RATE = metrics.SAMPLE_RATE_HZ


def _rng() -> np.random.Generator:
    return np.random.default_rng(20260819)


def _delay(signal: np.ndarray, samples: int) -> np.ndarray:
    """Return ``signal`` delayed by ``samples``, same length."""
    return np.concatenate([np.zeros(samples), signal])[: len(signal)]


def _percussive(seconds: float = 4.0, hits_per_second: float = 4.0) -> np.ndarray:
    """Noise-burst train: broadband, transient-rich, unambiguously alignable."""
    rng = _rng()
    length = int(seconds * SAMPLE_RATE)
    signal = rng.standard_normal(length) * 0.02
    step = int(SAMPLE_RATE / hits_per_second)
    for index, start in enumerate(range(0, length - step, step)):
        # Irregular spacing so the train is not itself quasi-periodic.
        offset = start + (index * 137) % 400
        burst = int(0.05 * SAMPLE_RATE)
        envelope = np.exp(-np.linspace(0, 12, burst))
        signal[offset : offset + burst] += rng.standard_normal(burst) * envelope
    return signal / np.max(np.abs(signal))


# ----- amendment 4: LSD floors relative to the reference peak ----------------


def test_lsd_of_a_signal_against_itself_is_zero() -> None:
    """If LSD of a signal against itself is not 0 dB then broken."""
    signal = _percussive(2.0)
    assert metrics.log_spectral_distance(signal, signal) == pytest.approx(0.0, abs=1e-9)


def test_lsd_relative_floor_survives_a_40db_level_change() -> None:
    """If a quieter copy of the same spectrum scores differently then the floor is absolute."""
    signal = _percussive(2.0)
    quiet = signal * (10.0 ** (-40.0 / 20.0))
    assert metrics.log_spectral_distance(quiet, quiet) == pytest.approx(0.0, abs=1e-9)


def test_lsd_relative_floor_beats_an_absolute_floor_on_empty_bins() -> None:
    """If empty-bin numerical noise dominates LSD then the floor is not relative.

    Amendment 4's calibration: the same null test read 39.7 dB absolute-floored
    and 1.14 dB relative-floored. This reproduces the mechanism -- a band-limited
    reference whose upper bins are ~0, perturbed only by dither far below the
    reference peak.
    """
    rng = _rng()
    length = 2 * SAMPLE_RATE
    spectrum = np.fft.rfft(rng.standard_normal(length))
    spectrum[len(spectrum) // 4 :] = 0.0  # a genuinely empty upper band
    reference = np.fft.irfft(spectrum, n=length)
    reference = reference / np.max(np.abs(reference))
    dithered = reference + rng.standard_normal(length) * 1e-7

    relative = metrics.log_spectral_distance(reference, dithered)

    fft_samples = int(round(metrics.LSD_FRAME_MS * SAMPLE_RATE / 1000.0))
    hop = fft_samples // 2
    ref_spectrum = metrics.stft_magnitude(reference, fft_samples, hop)
    test_spectrum = metrics.stft_magnitude(dithered, fft_samples, hop)
    frames = min(ref_spectrum.shape[0], test_spectrum.shape[0])
    absolute_floor = 10.0 ** (-100.0 / 20.0)
    ref_db = 20.0 * np.log10(np.maximum(ref_spectrum[:frames], absolute_floor))
    test_db = 20.0 * np.log10(np.maximum(test_spectrum[:frames], absolute_floor))
    absolute = float(np.mean(np.sqrt(np.mean(np.square(ref_db - test_db), axis=1))))

    assert relative < 1.0, f"relative-floored LSD should stay near 0, read {relative:.2f} dB"
    assert absolute > 5.0 * max(relative, 1e-6), (
        f"absolute floor {absolute:.2f} dB should be dominated by empty-bin noise "
        f"while the relative floor reads {relative:.2f} dB"
    )


# ----- amendment 5: onset hop resolution guard ------------------------------


def test_librosa_default_hop_raises_against_a_12ms_gate() -> None:
    """If hop 512 does not raise against a 12 ms gate then the guard is missing."""
    with pytest.raises(metrics.HopResolutionError, match="exceeds gate/4"):
        metrics.assert_onset_hop_resolution(512, SAMPLE_RATE, 12.0)


def test_hop_64_is_accepted_and_reports_its_resolution() -> None:
    """If hop 64 does not read 1.451 ms then the resolution maths is wrong."""
    assert metrics.assert_onset_hop_resolution(64, SAMPLE_RATE, 12.0) == pytest.approx(1.451, abs=1e-3)


def test_hop_guard_boundary_is_exactly_gate_over_four() -> None:
    """If the guard does not flip between 2.993 ms and 3.016 ms then it is not gate/4."""
    metrics.assert_onset_hop_resolution(132, SAMPLE_RATE, 12.0)
    with pytest.raises(metrics.HopResolutionError):
        metrics.assert_onset_hop_resolution(134, SAMPLE_RATE, 12.0)


def test_onset_strength_refuses_a_dead_low_band_fft() -> None:
    """If a 512-point FFT is accepted then the bass-onset guard is missing."""
    with pytest.raises(metrics.LowBandResolutionError):
        metrics.assert_low_band_resolution(512, SAMPLE_RATE)
    metrics.assert_low_band_resolution(2048, SAMPLE_RATE)


# ----- amendment 7: alignment on the envelope-rise carrier -------------------


@pytest.mark.parametrize("true_delay", [0, 64, 300, 1234, 5292])
def test_known_delay_is_recovered_on_transient_rich_material(true_delay: int) -> None:
    """If a known integer delay is not recovered within 1 ms then alignment is broken."""
    reference = _percussive(4.0)
    rendered = _delay(reference, true_delay)
    alignment = metrics.estimate_delay_samples(reference, rendered, label="synthetic")
    assert abs(alignment.delay_samples - true_delay) <= metrics.ALIGN_FINE_HOP_SAMPLES * 2, (
        f"expected ~{true_delay}, got {alignment.delay_samples} at corr {alignment.correlation:.4f}"
    )
    assert alignment.correlation > metrics.ALIGN_CORRELATION_FLOOR
    assert alignment.carrier == "half-wave-rectified envelope rise"


def test_alignment_carrier_is_never_the_raw_waveform() -> None:
    """If alignment reports any carrier but envelope rise then amendment 7 is violated."""
    reference = _percussive(2.0)
    alignment = metrics.estimate_delay_samples(reference, _delay(reference, 128))
    assert "envelope rise" in alignment.carrier
    assert "waveform" not in alignment.carrier


def test_pure_sine_never_returns_a_confident_wrong_lag() -> None:
    """If a delayed pure sine yields a wrong lag instead of a refusal then it cycle-skipped.

    Raw-waveform correlation returns lag 100 for a true lag of 300 at corr
    0.9995 here. Refusing is correct; a wrong number at high confidence is the
    defect.
    """
    true_delay = 300
    time = np.arange(2 * SAMPLE_RATE) / SAMPLE_RATE
    reference = np.sin(2 * math.pi * 1000.0 * time)
    rendered = _delay(reference, true_delay)
    try:
        alignment = metrics.estimate_delay_samples(reference, rendered, label="pure-sine")
    except metrics.AlignmentError:
        return
    assert abs(alignment.delay_samples - true_delay) <= 64, (
        f"cycle skip: returned {alignment.delay_samples} for a true delay of {true_delay} "
        f"at corr {alignment.correlation:.4f}"
    )


def test_transient_free_crescendo_never_returns_a_confident_wrong_lag() -> None:
    """If a delayed crescendo yields a wrong lag instead of a refusal then it cycle-skipped.

    Raw-waveform correlation returns -101 at corr 0.9996 on this shape.
    """
    true_delay = 300
    time = np.arange(3 * SAMPLE_RATE) / SAMPLE_RATE
    reference = np.sin(2 * math.pi * 440.0 * time) * np.linspace(0.001, 1.0, len(time))
    rendered = _delay(reference, true_delay)
    try:
        alignment = metrics.estimate_delay_samples(reference, rendered, label="crescendo")
    except metrics.AlignmentError:
        return
    assert abs(alignment.delay_samples - true_delay) <= 64, (
        f"cycle skip: returned {alignment.delay_samples} at corr {alignment.correlation:.4f}"
    )


def test_independent_noise_is_refused_not_aligned() -> None:
    """If two independent noise signals produce a lag then the correlation floor is missing.

    Independent signals 'align' at corr ~0.005 and would otherwise be reported
    as a row.
    """
    rng = _rng()
    left = rng.standard_normal(2 * SAMPLE_RATE)
    right = np.random.default_rng(999).standard_normal(2 * SAMPLE_RATE)
    with pytest.raises(metrics.AlignmentError):
        metrics.estimate_delay_samples(left, right, label="independent-noise")


def _click_train(period_samples: int, seconds: float = 4.0) -> np.ndarray:
    length = int(seconds * SAMPLE_RATE)
    signal = np.zeros(length)
    burst = 200
    envelope = np.exp(-np.linspace(0, 8, burst))
    for start in range(0, length - burst, period_samples):
        signal[start : start + burst] += envelope
    return signal


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
    signal = _click_train(period)
    alignment = metrics.estimate_delay_samples(signal, _delay(signal, period), label="16ths-at-122")

    # A periodic signal delayed by exactly one period coincides with itself, so
    # lag 0 is the honest answer and no algorithm can report 5422 here. That is
    # precisely why a bound check cannot prove a trim: this row would sail
    # through +-25 ms while hiding a whole period of displacement.
    assert alignment.delay_samples == pytest.approx(0, abs=64)
    assert abs(alignment.runner_up_delay_samples) == pytest.approx(period, abs=metrics.ONSET_HOP_SAMPLES), (
        "the one-period alias must surface as the runner-up lag, otherwise the row "
        f"hides it; read {alignment.runner_up_delay_samples}"
    )
    assert 0.5 < alignment.peak_ratio < metrics.ALIGN_AMBIGUITY_MAX_RATIO, (
        f"the alias should be a visible but non-refusing runner-up, ratio read {alignment.peak_ratio:.3f}"
    )


def test_indistinguishable_peaks_are_refused() -> None:
    """If two equally-good lags do not raise then the correlation is reporting a guess.

    A click train whose period is an exact whole number of coarse frames
    correlates identically at lag 0 and at one period, so no delay is
    identified and the only honest answer is a refusal.
    """
    period = metrics.ONSET_HOP_SAMPLES * 85
    signal = _click_train(period)
    with pytest.raises(metrics.AlignmentAmbiguousError, match="does not identify a delay"):
        metrics.estimate_delay_samples(signal, _delay(signal, period), label="exact-period")


def test_flat_carrier_is_refused_as_degenerate() -> None:
    """If a constant-amplitude signal yields an alignment then the carrier guard is missing."""
    constant = np.ones(SAMPLE_RATE) * 0.5
    with pytest.raises(metrics.DegenerateCarrierError):
        metrics.envelope_rise_carrier(constant, metrics.ONSET_HOP_SAMPLES, label="constant")


def test_apply_delay_lines_the_signals_up() -> None:
    """If applying the measured delay does not restore sample alignment then broken."""
    reference = _percussive(2.0)
    rendered = _delay(reference, 512)
    alignment = metrics.estimate_delay_samples(reference, rendered)
    left, right = metrics.apply_delay(reference, rendered, 512)
    assert np.allclose(left, right, atol=1e-12)
    assert abs(alignment.delay_samples - 512) <= metrics.ALIGN_FINE_HOP_SAMPLES * 2


# ----- non-silence and channel distinctness ---------------------------------


def test_digital_silence_is_refused() -> None:
    """If a silent excerpt is accepted then an evicted iCloud stub would be measured."""
    with pytest.raises(metrics.SilentExcerptError):
        metrics.assert_loud_non_silence(np.zeros((2, SAMPLE_RATE)), "evicted-stub")


def test_very_quiet_excerpt_is_refused() -> None:
    """If a -70 dBFS excerpt passes the loudness assert then the threshold is too low."""
    quiet = _rng().standard_normal((2, SAMPLE_RATE)) * (10.0 ** (-70.0 / 20.0))
    with pytest.raises(metrics.SilentExcerptError):
        metrics.assert_loud_non_silence(quiet, "too-quiet")


def test_loud_excerpt_reports_its_levels() -> None:
    """If a loud excerpt does not pass and report both levels then the assert is wrong."""
    loud = np.stack([_percussive(1.0), _percussive(1.0)])
    rms_dbfs, peak_dbfs = metrics.assert_loud_non_silence(loud, "loud")
    assert rms_dbfs > metrics.NON_SILENCE_MIN_RMS_DBFS
    assert peak_dbfs > metrics.NON_SILENCE_MIN_PEAK_DBFS


def test_mono_collapse_is_caught_by_distinctness_not_channel_count() -> None:
    """If a duplicated-channel render passes then only channel count is being checked."""
    rng = _rng()
    reference = np.stack([rng.standard_normal(1000), rng.standard_normal(1000)])
    collapsed = np.stack([reference[0], reference[0]])
    assert collapsed.shape[0] == reference.shape[0] == 2  # a count check would pass here
    with pytest.raises(metrics.ChannelCollapseError):
        metrics.assert_channel_distinctness_preserved(reference, collapsed, "collapsed")


def test_distinct_render_passes_and_mono_source_is_inapplicable() -> None:
    """If a mono source is not reported inapplicable then the check is misleading."""
    rng = _rng()
    reference = np.stack([rng.standard_normal(1000), rng.standard_normal(1000)])
    rendered = np.stack([rng.standard_normal(1000), rng.standard_normal(1000)])
    assert metrics.assert_channel_distinctness_preserved(reference, rendered, "ok") is False
    mono = np.stack([reference[0], reference[0]])
    assert metrics.assert_channel_distinctness_preserved(mono, mono, "mono") is True


# ----- residual diagnostic --------------------------------------------------


def test_residual_matches_the_rho_identity() -> None:
    """If residual dBr does not equal 10log10(1-rho^2) then the diagnostic is wrong."""
    rng = _rng()
    reference = _percussive(2.0)
    rendered = reference * 0.7 + rng.standard_normal(len(reference)) * 0.1
    residual = metrics.residual_dbr(reference, rendered)
    expected = 10.0 * math.log10(1.0 - residual.correlation_rho**2)
    assert residual.residual_dbr == pytest.approx(expected, abs=1e-9)


def test_identical_signals_give_a_floor_residual() -> None:
    """If a signal against itself is not ~-300 dBr then the gain fit is broken."""
    signal = _percussive(1.0)
    assert metrics.residual_dbr(signal, signal).residual_dbr < -100.0


# ----- onsets ---------------------------------------------------------------


def test_onsets_are_found_and_a_shifted_copy_displaces_by_the_shift() -> None:
    """If a 5 ms shift is not measured as ~5 ms displacement then onset timing is broken."""
    reference = _percussive(4.0)
    shift = int(0.005 * SAMPLE_RATE)
    comparison = metrics.compare_onsets(reference, _delay(reference, shift))
    assert comparison.reference_onsets > 5
    assert comparison.displacement_median_ms == pytest.approx(5.0, abs=1.5)
    assert comparison.recovery_gate_ms == metrics.ONSET_RECOVERY_GATE_MS


def test_identical_signals_recover_every_onset() -> None:
    """If a signal against itself does not recover 100 percent of onsets then broken."""
    reference = _percussive(4.0)
    comparison = metrics.compare_onsets(reference, reference)
    assert comparison.recovery_fraction == pytest.approx(1.0)
    assert comparison.displacement_p95_ms == pytest.approx(0.0, abs=1e-9)


# ----- PCM sidecars ---------------------------------------------------------


def test_pcm_roundtrip_preserves_samples_and_pins_sha256(tmp_path) -> None:
    """If a PCM roundtrip loses samples or skips its sha256 check then broken."""
    samples = np.stack([_percussive(0.5), _percussive(0.5) * 0.5]).astype(np.float32)
    path = tmp_path / "excerpt.f32"
    meta = metrics.write_pcm(path, samples)
    restored, restored_meta = metrics.read_pcm(path)
    assert restored_meta.sha256 == meta.sha256
    assert restored.shape == samples.shape
    assert np.allclose(restored, samples, atol=1e-7)


def test_tampered_pcm_is_refused(tmp_path) -> None:
    """If editing a PCM file without its sidecar goes unnoticed then the pin is decorative."""
    samples = np.stack([_percussive(0.5), _percussive(0.5)]).astype(np.float32)
    path = tmp_path / "excerpt.f32"
    metrics.write_pcm(path, samples)
    path.write_bytes(path.read_bytes()[:-8])
    with pytest.raises(metrics.HarnessIntegrityError, match="sha256 mismatch"):
        metrics.read_pcm(path)


def test_pcm_sidecar_metadata_is_machine_readable(tmp_path) -> None:
    """If the PCM sidecar is not valid JSON with rate/channels/frames then broken."""
    samples = np.stack([_percussive(0.25), _percussive(0.25)]).astype(np.float32)
    path = tmp_path / "excerpt.f32"
    metrics.write_pcm(path, samples)
    payload = json.loads(path.with_suffix(".f32.json").read_text(encoding="utf-8"))
    assert payload["sample_rate_hz"] == SAMPLE_RATE
    assert payload["channels"] == 2
    assert payload["frames"] == samples.shape[1]


# ----- amendment 8: master tempo and key-shift accuracy ---------------------


def _tone(hz: float, seconds: float = 3.0, harmonics: int = 5) -> np.ndarray:
    """A harmonic-rich pitched tone -- a bare sine is not what f0 trackers meet."""
    time = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    signal = sum(np.sin(2 * math.pi * hz * n * time) / n for n in range(1, harmonics + 1))
    return signal / np.max(np.abs(signal))


def _cents_to_ratio(cents: float) -> float:
    return 2.0 ** (cents / 1200.0)


def test_f0_tracker_reads_a_known_tone() -> None:
    """If a 220 Hz tone is not tracked to within 1 cent then the tracker is broken."""
    f0, confidence = metrics.track_f0(_tone(220.0))
    voiced = np.isfinite(f0)
    assert voiced.mean() > 0.9, f"only {voiced.mean():.1%} of frames voiced"
    measured = float(np.median(f0[voiced]))
    error_cents = 1200.0 * math.log2(measured / 220.0)
    assert abs(error_cents) < 1.0, f"tracked {measured:.3f} Hz, {error_cents:+.2f} cents off"
    assert float(np.median(confidence[voiced])) > metrics.PITCH_CONFIDENCE_FLOOR


def test_master_tempo_promise_holds_when_pitch_is_unchanged() -> None:
    """If an unshifted render at rate 1.08 does not read ~0 cents then broken."""
    reference = _tone(220.0, 4.0)
    # A faithful master-tempo render: same pitch, time-scaled by 1/rate.
    rate = 1.08
    rendered = _tone(220.0, 4.0 / rate)
    error = metrics.pitch_error_cents(reference, rendered, rate=rate, semitones=0)
    assert abs(error.p50_cents) < 2.0, f"read {error.p50_cents:+.2f} cents"
    assert error.expected_cents == 0.0


def test_thirty_cent_drift_at_rate_1_15_is_surfaced() -> None:
    """If a 30-cent drift at rate 1.15 is not measured as ~30 cents then broken.

    This is the exact failure amendment 8 names: a stretcher drifting 30 cents
    at rate 1.15 otherwise shows up only as unattributed spectral divergence.
    """
    rate = 1.15
    reference = _tone(220.0, 4.0)
    drifted = _tone(220.0 * _cents_to_ratio(30.0), 4.0 / rate)
    error = metrics.pitch_error_cents(reference, drifted, rate=rate, semitones=0)
    assert error.p50_cents == pytest.approx(30.0, abs=3.0), f"read {error.p50_cents:+.2f} cents"


def test_expected_shift_is_rate_independent() -> None:
    """If the expectation moves with rate then the master-tempo promise is not being tested."""
    reference = _tone(220.0, 4.0)
    for rate in (0.92, 1.0, 1.16):
        rendered = _tone(220.0, 4.0 / rate)
        error = metrics.pitch_error_cents(reference, rendered, rate=rate, semitones=0)
        assert error.expected_cents == 0.0
        assert abs(error.p50_cents) < 3.0, f"rate {rate} read {error.p50_cents:+.2f} cents"


def test_commanded_key_shift_expects_exactly_a_hundred_cents_per_semitone() -> None:
    """If a correct +2 semitone shift does not read ~0 error then the expectation is wrong."""
    reference = _tone(220.0, 4.0)
    shifted = _tone(220.0 * _cents_to_ratio(200.0), 4.0)
    error = metrics.pitch_error_cents(reference, shifted, rate=1.0, semitones=2)
    assert error.expected_cents == 200.0
    assert abs(error.p50_cents) < 2.0, f"read {error.p50_cents:+.2f} cents"


def test_key_shift_that_lands_flat_is_reported_signed() -> None:
    """If a shift 40 cents flat of +2 semitones is not reported as ~-40 then broken."""
    reference = _tone(220.0, 4.0)
    shifted = _tone(220.0 * _cents_to_ratio(160.0), 4.0)
    error = metrics.pitch_error_cents(reference, shifted, rate=1.0, semitones=2)
    assert error.p50_cents == pytest.approx(-40.0, abs=3.0), f"read {error.p50_cents:+.2f}"


def test_unpitched_material_is_refused_not_scored() -> None:
    """If noise yields a cents figure then the voiced-fraction guard is missing."""
    rng = _rng()
    noise = rng.standard_normal(3 * SAMPLE_RATE)
    other = np.random.default_rng(7).standard_normal(3 * SAMPLE_RATE)
    with pytest.raises(metrics.UnpitchedMaterialError):
        metrics.pitch_error_cents(noise, other, rate=1.0, semitones=0)
