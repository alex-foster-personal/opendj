"""Regression tests for the stretch-quality analysis primitives.

Alignment has its own module (``test_stretch_alignment.py``) because amendment
7 carries a four-gate stack and its own adversarial probe set. What is here is
everything else: the LSD floor (amendment 4), onset resolution and the
low-band basis (amendments 5 and 9), the sha256-pinned PCM sidecars, the
channel-distinctness and non-silence asserts, the residual diagnostic, and the
master-tempo/key-shift accuracy of amendment 8.

One-line contract per test, in the house "if X does/doesn't Y then broken"
form, is carried in each test's docstring.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from scripts.quality import metrics, probes

SAMPLE_RATE = metrics.SAMPLE_RATE_HZ

# ----- amendment 4: LSD floors relative to the reference peak ----------------


def test_lsd_of_a_signal_against_itself_is_zero() -> None:
    """If LSD of a signal against itself is not 0 dB then broken."""
    signal = probes.percussive(2.0)
    assert metrics.log_spectral_distance(signal, signal) == pytest.approx(0.0, abs=1e-9)


def test_lsd_relative_floor_survives_a_40db_level_change() -> None:
    """If a quieter copy of the same spectrum scores differently then the floor is absolute."""
    signal = probes.percussive(2.0)
    quiet = signal * (10.0 ** (-40.0 / 20.0))
    assert metrics.log_spectral_distance(quiet, quiet) == pytest.approx(0.0, abs=1e-9)


def test_lsd_relative_floor_beats_an_absolute_floor_on_empty_bins() -> None:
    """If empty-bin numerical noise dominates LSD then the floor is not relative.

    Amendment 4's calibration: the same null test read 39.7 dB absolute-floored
    and 1.14 dB relative-floored. This reproduces the mechanism -- a band-limited
    reference whose upper bins are ~0, perturbed only by dither far below the
    reference peak.
    """
    rng = probes.rng()
    length = 2 * SAMPLE_RATE
    spectrum = np.fft.rfft(rng.standard_normal(length))
    spectrum[len(spectrum) // 4 :] = 0.0  # a genuinely empty upper band
    reference = np.fft.irfft(spectrum, n=length)
    reference = reference / np.max(np.abs(reference))
    dithered = reference + rng.standard_normal(length) * 1e-7

    relative = metrics.log_spectral_distance(reference, dithered)

    fft_samples = round(metrics.LSD_FRAME_MS * SAMPLE_RATE / 1000.0)
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
    assert metrics.assert_onset_hop_resolution(64, SAMPLE_RATE, 12.0) == pytest.approx(
        1.451, abs=1e-3
    )


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


def test_low_band_transient_is_detected_not_swallowed() -> None:
    """If a bass-only transient produces no onset then the detector is deaf to the DJ case.

    Amendment 9, this lane's own-basis verification. librosa's onset_strength
    at n_fft 512 / n_mels 128 silently builds dead low-frequency mel bands and
    swallows the warning, so its onset numbers are invalid for ranking; fixing
    it moved lane A's baseline p95 at rate 1.08 from 3.2 ms to 19.2 ms. This
    module uses LINEAR bins and no mel basis at all, so the defect has no way
    in -- but "cannot happen" is worth exactly as much as the test that shows
    it, so this asserts the observable property: a 60 Hz kick, with nothing
    above 120 Hz anywhere in the signal, is still found at its true time.
    """
    hit_times = (0.5, 1.5, 2.5, 3.5)
    signal = probes.low_band_kick(hit_times)

    spectrum = np.abs(np.fft.rfft(signal))
    frequencies = np.fft.rfftfreq(len(signal), 1.0 / SAMPLE_RATE)
    total = float(np.sum(np.square(spectrum)))
    above_250 = float(np.sum(np.square(spectrum[frequencies > 250.0]))) / total
    assert above_250 < 1e-3, (
        f"the probe carries {above_250:.4%} of its energy above 250 Hz, so passing it "
        "would not prove low-band sensitivity"
    )

    found = metrics.pick_onsets(
        metrics.onset_strength(signal), SAMPLE_RATE, metrics.ONSET_HOP_SAMPLES
    )
    for hit in hit_times:
        assert np.min(np.abs(found - hit)) < 0.05, f"no onset near the {hit}s kick: {found}"


# ----- non-silence and channel distinctness ---------------------------------


def test_digital_silence_is_refused() -> None:
    """If a silent excerpt is accepted then an evicted iCloud stub would be measured."""
    with pytest.raises(metrics.SilentExcerptError):
        metrics.assert_loud_non_silence(np.zeros((2, SAMPLE_RATE)), "evicted-stub")


def test_very_quiet_excerpt_is_refused() -> None:
    """If a -70 dBFS excerpt passes the loudness assert then the threshold is too low."""
    quiet = probes.rng().standard_normal((2, SAMPLE_RATE)) * (10.0 ** (-70.0 / 20.0))
    with pytest.raises(metrics.SilentExcerptError):
        metrics.assert_loud_non_silence(quiet, "too-quiet")


def test_loud_excerpt_reports_its_levels() -> None:
    """If a loud excerpt does not pass and report both levels then the assert is wrong."""
    loud = np.stack([probes.percussive(1.0), probes.percussive(1.0)])
    rms_dbfs, peak_dbfs = metrics.assert_loud_non_silence(loud, "loud")
    assert rms_dbfs > metrics.NON_SILENCE_MIN_RMS_DBFS
    assert peak_dbfs > metrics.NON_SILENCE_MIN_PEAK_DBFS


def test_mono_collapse_is_caught_by_distinctness_not_channel_count() -> None:
    """If a duplicated-channel render passes then only channel count is being checked."""
    rng = probes.rng()
    reference = np.stack([rng.standard_normal(1000), rng.standard_normal(1000)])
    collapsed = np.stack([reference[0], reference[0]])
    assert collapsed.shape[0] == reference.shape[0] == 2  # a count check would pass here
    with pytest.raises(metrics.ChannelCollapseError):
        metrics.assert_channel_distinctness_preserved(reference, collapsed, "collapsed")


def test_distinct_render_passes_and_mono_source_is_inapplicable() -> None:
    """If a mono source is not reported inapplicable then the check is misleading."""
    rng = probes.rng()
    reference = np.stack([rng.standard_normal(1000), rng.standard_normal(1000)])
    rendered = np.stack([rng.standard_normal(1000), rng.standard_normal(1000)])
    assert metrics.assert_channel_distinctness_preserved(reference, rendered, "ok") is False
    mono = np.stack([reference[0], reference[0]])
    assert metrics.assert_channel_distinctness_preserved(mono, mono, "mono") is True


# ----- residual diagnostic --------------------------------------------------


def test_residual_matches_the_rho_identity() -> None:
    """If residual dBr does not equal 10log10(1-rho^2) then the diagnostic is wrong."""
    rng = probes.rng()
    reference = probes.percussive(2.0)
    rendered = reference * 0.7 + rng.standard_normal(len(reference)) * 0.1
    residual = metrics.residual_dbr(reference, rendered)
    expected = 10.0 * math.log10(1.0 - residual.correlation_rho**2)
    assert residual.residual_dbr == pytest.approx(expected, abs=1e-9)


def test_identical_signals_give_a_floor_residual() -> None:
    """If a signal against itself is not ~-300 dBr then the gain fit is broken."""
    signal = probes.percussive(1.0)
    assert metrics.residual_dbr(signal, signal).residual_dbr < -100.0


# ----- onsets ---------------------------------------------------------------


def test_onsets_are_found_and_a_shifted_copy_displaces_by_the_shift() -> None:
    """If a 5 ms shift is not measured as ~5 ms displacement then onset timing is broken."""
    reference = probes.percussive(4.0)
    shift = int(0.005 * SAMPLE_RATE)
    comparison = metrics.compare_onsets(reference, probes.delay(reference, shift))
    assert comparison.reference_onsets > 5
    assert comparison.displacement_median_ms == pytest.approx(5.0, abs=1.5)
    assert comparison.recovery_gate_ms == metrics.ONSET_RECOVERY_GATE_MS


def test_identical_signals_recover_every_onset() -> None:
    """If a signal against itself does not recover 100 percent of onsets then broken."""
    reference = probes.percussive(4.0)
    comparison = metrics.compare_onsets(reference, reference)
    assert comparison.recovery_fraction == pytest.approx(1.0)
    assert comparison.displacement_p95_ms == pytest.approx(0.0, abs=1e-9)


# ----- PCM sidecars ---------------------------------------------------------


def test_pcm_roundtrip_preserves_samples_and_pins_sha256(tmp_path) -> None:
    """If a PCM roundtrip loses samples or skips its sha256 check then broken."""
    samples = np.stack([probes.percussive(0.5), probes.percussive(0.5) * 0.5]).astype(np.float32)
    path = tmp_path / "excerpt.f32"
    meta = metrics.write_pcm(path, samples)
    restored, restored_meta = metrics.read_pcm(path)
    assert restored_meta.sha256 == meta.sha256
    assert restored.shape == samples.shape
    assert np.allclose(restored, samples, atol=1e-7)


def test_tampered_pcm_is_refused(tmp_path) -> None:
    """If editing a PCM file without its sidecar goes unnoticed then the pin is decorative."""
    samples = np.stack([probes.percussive(0.5), probes.percussive(0.5)]).astype(np.float32)
    path = tmp_path / "excerpt.f32"
    metrics.write_pcm(path, samples)
    path.write_bytes(path.read_bytes()[:-8])
    with pytest.raises(metrics.HarnessIntegrityError, match="sha256 mismatch"):
        metrics.read_pcm(path)


def test_pcm_sidecar_metadata_is_machine_readable(tmp_path) -> None:
    """If the PCM sidecar is not valid JSON with rate/channels/frames then broken."""
    samples = np.stack([probes.percussive(0.25), probes.percussive(0.25)]).astype(np.float32)
    path = tmp_path / "excerpt.f32"
    metrics.write_pcm(path, samples)
    payload = json.loads(path.with_suffix(".f32.json").read_text(encoding="utf-8"))
    assert payload["sample_rate_hz"] == SAMPLE_RATE
    assert payload["channels"] == 2
    assert payload["frames"] == samples.shape[1]


# ----- amendment 8: master tempo and key-shift accuracy ---------------------


def test_f0_tracker_reads_a_known_tone() -> None:
    """If a 220 Hz tone is not tracked to within 1 cent then the tracker is broken."""
    f0, confidence = metrics.track_f0(probes.harmonic_tone(220.0))
    voiced = np.isfinite(f0)
    assert voiced.mean() > 0.9, f"only {voiced.mean():.1%} of frames voiced"
    measured = float(np.median(f0[voiced]))
    error_cents = 1200.0 * math.log2(measured / 220.0)
    assert abs(error_cents) < 1.0, f"tracked {measured:.3f} Hz, {error_cents:+.2f} cents off"
    assert float(np.median(confidence[voiced])) > metrics.PITCH_CONFIDENCE_FLOOR


def test_master_tempo_promise_holds_when_pitch_is_unchanged() -> None:
    """If an unshifted render at rate 1.08 does not read ~0 cents then broken."""
    reference = probes.harmonic_tone(220.0, 4.0)
    # A faithful master-tempo render: same pitch, time-scaled by 1/rate.
    rate = 1.08
    rendered = probes.harmonic_tone(220.0, 4.0 / rate)
    error = metrics.pitch_error_cents(reference, rendered, rate=rate, semitones=0)
    assert abs(error.p50_cents) < 2.0, f"read {error.p50_cents:+.2f} cents"
    assert error.expected_cents == 0.0


def test_thirty_cent_drift_at_rate_1_15_is_surfaced() -> None:
    """If a 30-cent drift at rate 1.15 is not measured as ~30 cents then broken.

    This is the exact failure amendment 8 names: a stretcher drifting 30 cents
    at rate 1.15 otherwise shows up only as unattributed spectral divergence.
    """
    rate = 1.15
    reference = probes.harmonic_tone(220.0, 4.0)
    drifted = probes.harmonic_tone(220.0 * probes.cents_to_ratio(30.0), 4.0 / rate)
    error = metrics.pitch_error_cents(reference, drifted, rate=rate, semitones=0)
    assert error.p50_cents == pytest.approx(30.0, abs=3.0), f"read {error.p50_cents:+.2f} cents"


def test_expected_shift_is_rate_independent() -> None:
    """If the expectation moves with rate then the master-tempo promise is not being tested."""
    reference = probes.harmonic_tone(220.0, 4.0)
    for rate in (0.92, 1.0, 1.16):
        rendered = probes.harmonic_tone(220.0, 4.0 / rate)
        error = metrics.pitch_error_cents(reference, rendered, rate=rate, semitones=0)
        assert error.expected_cents == 0.0
        assert abs(error.p50_cents) < 3.0, f"rate {rate} read {error.p50_cents:+.2f} cents"


def test_commanded_key_shift_expects_exactly_a_hundred_cents_per_semitone() -> None:
    """If a correct +2 semitone shift does not read ~0 error then the expectation is wrong."""
    reference = probes.harmonic_tone(220.0, 4.0)
    shifted = probes.harmonic_tone(220.0 * probes.cents_to_ratio(200.0), 4.0)
    error = metrics.pitch_error_cents(reference, shifted, rate=1.0, semitones=2)
    assert error.expected_cents == 200.0
    assert abs(error.p50_cents) < 2.0, f"read {error.p50_cents:+.2f} cents"


def test_key_shift_that_lands_flat_is_reported_signed() -> None:
    """If a shift 40 cents flat of +2 semitones is not reported as ~-40 then broken."""
    reference = probes.harmonic_tone(220.0, 4.0)
    shifted = probes.harmonic_tone(220.0 * probes.cents_to_ratio(160.0), 4.0)
    error = metrics.pitch_error_cents(reference, shifted, rate=1.0, semitones=2)
    assert error.p50_cents == pytest.approx(-40.0, abs=3.0), f"read {error.p50_cents:+.2f}"


def test_unpitched_material_is_refused_not_scored() -> None:
    """If noise yields a cents figure then the voiced-fraction guard is missing."""
    rng = probes.rng()
    noise = rng.standard_normal(3 * SAMPLE_RATE)
    other = np.random.default_rng(7).standard_normal(3 * SAMPLE_RATE)
    with pytest.raises(metrics.UnpitchedMaterialError):
        metrics.pitch_error_cents(noise, other, rate=1.0, semitones=0)
