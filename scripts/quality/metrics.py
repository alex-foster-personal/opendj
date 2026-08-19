"""Analysis primitives for the stretch-quality harness.

Binding spec: ``.planning/QUALITY-METHODOLOGY-RECONCILED.md``. Every amendment
that constrains this module is named at its implementation site.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 Align renders by measured integer delay on the half-wave-rectified
    envelope-RISE carrier at BOTH stages, never on raw waveform (amendment 7).
    [if] a pure unmodulated sine is aligned [then ⛔️] the call raises rather
      than returning the cycle-skipped lag at high reported confidence
    [if] two independent noise signals are aligned [then ⛔️] the correlation
      floor raises instead of reporting a lag
    [if] the fine-stage argmax lands on its search bound [then ⛔️] it raises
  ✔︎ ✅ 🎯 Floor LSD bins 80 dB below the REFERENCE PEAK, never absolute
    (amendment 4).
    [if] a signal is compared against itself [then] LSD is 0 dB, not the
      empty-bin numerical noise an absolute floor produces
  ✔︎ ✅ 🎯 Raise when onset envelope resolution exceeds gate/4 (amendment 5).
    [if] hop 512 is used against a 12 ms gate [then ⛔️] it raises
    [if] hop 64 is used against a 12 ms gate [then] it is accepted
  ✔︎ ✅ 🎯 Assert loud non-silence on every excerpt.
    [if] an iCloud-evicted stub decodes to silence [then ⛔️] it raises
  ✔︎ ✅ 🎯 Assert channel DISTINCTNESS is preserved, not channel count.
    [if] a stereo source renders to bit-identical channels [then ⛔️] it raises

No librosa: the onset flux is a linear-frequency STFT, so the
``n_fft=512``/``n_mels=128`` dead-low-band trap cannot arise. Its structural
analogue is asserted instead by ``assert_low_band_resolution``.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

# ----- structural constants -------------------------------------------------
# These are HARNESS INTEGRITY values, not quality thresholds. Quality
# thresholds are set jointly across lanes after both harnesses run (spec
# preamble), and none are hard-coded anywhere in this package.

SAMPLE_RATE_HZ = 44_100

#: Amendment 5. Onset envelope hop. 64 samples = 1.451 ms at 44.1 kHz, which
#: is gate/8 against a 12 ms gate; librosa's default 512 would be 11.61 ms.
ONSET_HOP_SAMPLES = 64

#: PROPOSED, per lane A's harness. Not a pass/fail threshold: it is the
#: matching window that defines what "recovered" counts as, and it is reported
#: beside every recovery figure so a later joint decision can restate it.
ONSET_RECOVERY_GATE_MS = 12.0

#: Amendment 4. Relative to the REFERENCE PEAK, never absolute dBFS.
LSD_FLOOR_DB_BELOW_REF_PEAK = 80.0
LSD_FRAME_MS = 50.0

#: Amendment 7 guards.
ALIGN_COARSE_MAX_LAG_MS = 250.0
ALIGN_FINE_HOP_SAMPLES = 4
ALIGN_CORRELATION_FLOOR = 0.20
ALIGN_AMBIGUITY_GUARD_MS = 15.0
#: Raises only when the best and runner-up peaks are INDISTINGUISHABLE, i.e.
#: when the correlation genuinely does not identify a delay. It is deliberately
#: NOT set tight enough to refuse ordinary repetitive dance music: measured on
#: 16th-note material at 122 BPM the one-period alias reads 0.83 against a true
#: peak of 1.00, so a tight ratio would refuse four of the six fixtures. The
#: spec's instruction for that regime is that align_correlation (and here also
#: peak_ratio and the runner-up lag) is the DISCRIMINATING OBSERVABLE, reported
#: per row for joint threshold-setting, not a unilateral raise.
ALIGN_AMBIGUITY_MAX_RATIO = 0.98

#: Envelope RMS window for the alignment carrier (2.9 ms at 44.1 kHz).
ENVELOPE_WINDOW_SAMPLES = 128

#: A carrier whose rise is this flat relative to its own level carries no
#: alignment information (an unmodulated tone). Refusing is the point.
CARRIER_ACTIVITY_FLOOR = 1e-3

#: Loud-non-silence assert. An iCloud-evicted stub serves empty rather than
#: erroring, so it decodes to digital silence and would otherwise be measured.
NON_SILENCE_MIN_RMS_DBFS = -60.0
NON_SILENCE_MIN_PEAK_DBFS = -40.0

#: Structural analogue of the librosa mel dead-band defect.
LOW_BAND_MAX_BIN_HZ = 30.0
LOW_BAND_MIN_BINS_BELOW_250HZ = 8

ONSET_FLUX_FFT_SAMPLES = 2048
ONSET_MIN_SEPARATION_MS = 30.0
ONSET_MATCH_WINDOW_MS = 50.0
ONSET_PEAK_FACTOR = 1.5
ONSET_PEAK_DELTA = 0.02
ONSET_PEAK_MEDIAN_WINDOW_MS = 100.0


# ----- structural failures --------------------------------------------------
# Every one of these is a REFUSAL TO PRODUCE A NUMBER. None of them is a
# quality verdict; a raise means the instrument could not measure this row, so
# the row is reported as a failure rather than as a plausible-looking value.


class HarnessIntegrityError(RuntimeError):
    """Base for every structural refusal in this module."""


class HopResolutionError(HarnessIntegrityError):
    """Amendment 5: onset envelope resolution coarser than gate/4."""


class SilentExcerptError(HarnessIntegrityError):
    """The excerpt is silent or near-silent (an evicted-stub decode)."""


class ChannelCollapseError(HarnessIntegrityError):
    """A distinct-channel source rendered to duplicated channels."""


class LowBandResolutionError(HarnessIntegrityError):
    """The flux STFT cannot resolve the bass region."""


class AlignmentError(HarnessIntegrityError):
    """Base for alignment refusals (amendment 7)."""


class DegenerateCarrierError(AlignmentError):
    """The envelope-rise carrier holds no alignment information."""


class AlignmentCorrelationFloorError(AlignmentError):
    """Best alignment correlation is below the floor."""


class AlignmentBoundError(AlignmentError):
    """The fine-stage argmax landed on its search bound."""


class AlignmentAmbiguousError(AlignmentError):
    """A runner-up correlation peak rivals the best one.

    This is the guard for quasi-periodic material: the spec records that a
    120 ms-early render can alias onto an interior peak on 16th-note-periodic
    music at 103-158 BPM without tripping a +-25 ms bound, so the bound alone
    is not proof of correct alignment. Peak dominance is.
    """


# ----- PCM sidecars ---------------------------------------------------------


@dataclass(frozen=True)
class PcmMeta:
    """Sidecar metadata for one raw float32 PCM file."""

    sample_rate_hz: int
    channels: int
    frames: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_pcm(path: Path, samples: np.ndarray, sample_rate_hz: int = SAMPLE_RATE_HZ) -> PcmMeta:
    """Write ``samples`` (channels x frames) as interleaved little-endian f32."""
    if samples.ndim != 2:
        raise ValueError(f"expected a channels x frames array, got shape {samples.shape}")
    interleaved = np.ascontiguousarray(samples.T, dtype="<f4")
    payload = interleaved.tobytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    meta = PcmMeta(
        sample_rate_hz=sample_rate_hz,
        channels=int(samples.shape[0]),
        frames=int(samples.shape[1]),
        sha256=sha256_bytes(payload),
    )
    path.with_suffix(path.suffix + ".json").write_text(
        json.dumps(meta.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return meta


def read_pcm(path: Path) -> tuple[np.ndarray, PcmMeta]:
    """Read a PCM sidecar and RE-VERIFY its pinned sha256 before returning it."""
    payload = path.read_bytes()
    meta_path = path.with_suffix(path.suffix + ".json")
    if not meta_path.exists():
        raise HarnessIntegrityError(f"PCM sidecar has no metadata: {meta_path}")
    raw = json.loads(meta_path.read_text(encoding="utf-8"))
    meta = PcmMeta(
        sample_rate_hz=int(raw["sample_rate_hz"]),
        channels=int(raw["channels"]),
        frames=int(raw["frames"]),
        sha256=str(raw["sha256"]),
    )
    actual = sha256_bytes(payload)
    if actual != meta.sha256:
        raise HarnessIntegrityError(
            f"PCM sha256 mismatch for {path}: pinned {meta.sha256}, read {actual}"
        )
    samples = np.frombuffer(payload, dtype="<f4").reshape(meta.frames, meta.channels).T
    return np.ascontiguousarray(samples, dtype=np.float64), meta


def to_mono(samples: np.ndarray) -> np.ndarray:
    """Channel mean. Analysis carriers are mono; distinctness is checked apart."""
    if samples.ndim == 1:
        return np.asarray(samples, dtype=np.float64)
    return np.asarray(samples, dtype=np.float64).mean(axis=0)


# ----- excerpt asserts ------------------------------------------------------


def _dbfs(value: float) -> float:
    return -math.inf if value <= 0.0 else 20.0 * math.log10(value)


def assert_loud_non_silence(samples: np.ndarray, label: str) -> tuple[float, float]:
    """Raise unless the excerpt is audibly loud. Returns (rms_dbfs, peak_dbfs).

    An iCloud-evicted stub is served as an empty body rather than an error, so
    it decodes to digital silence. Without this assert the whole grid would be
    measured against silence and every metric would look excellent.
    """
    mono = to_mono(samples)
    if mono.size == 0:
        raise SilentExcerptError(f"{label}: excerpt is empty")
    rms_dbfs = _dbfs(float(np.sqrt(np.mean(np.square(mono)))))
    peak_dbfs = _dbfs(float(np.max(np.abs(mono))))
    if rms_dbfs < NON_SILENCE_MIN_RMS_DBFS or peak_dbfs < NON_SILENCE_MIN_PEAK_DBFS:
        raise SilentExcerptError(
            f"{label}: excerpt is not loud non-silence "
            f"(rms {rms_dbfs:.1f} dBFS, peak {peak_dbfs:.1f} dBFS; "
            f"need rms >= {NON_SILENCE_MIN_RMS_DBFS} and peak >= {NON_SILENCE_MIN_PEAK_DBFS})"
        )
    return rms_dbfs, peak_dbfs


def channels_are_bit_identical(samples: np.ndarray) -> bool:
    if samples.ndim != 2 or samples.shape[0] < 2:
        return True
    first = samples[0]
    return all(np.array_equal(first, samples[channel]) for channel in range(1, samples.shape[0]))


def assert_channel_distinctness_preserved(
    reference: np.ndarray, rendered: np.ndarray, label: str
) -> bool:
    """Raise if a distinct-channel source came back with duplicated channels.

    Checking channel COUNT is what lets a mono-collapsed render through: a
    renderer that duplicates its last channel still reports two. The observable
    that discriminates is whether the channels are still distinct.

    Returns True when the reference itself is mono-equivalent, i.e. when the
    check is inapplicable rather than passed.
    """
    if channels_are_bit_identical(reference):
        return True
    if channels_are_bit_identical(rendered):
        raise ChannelCollapseError(
            f"{label}: source channels are distinct but the render's are bit-identical "
            "(mono collapse, most likely silent last-channel duplication)"
        )
    return False


# ----- envelope-rise carrier (amendments 6 and 7) ---------------------------


def envelope_rms(signal: np.ndarray, hop_samples: int, window_samples: int) -> np.ndarray:
    """Sliding-RMS amplitude envelope at ``hop_samples``."""
    if hop_samples < 1 or window_samples < 1:
        raise ValueError("hop and window must be >= 1 sample")
    mono = np.asarray(signal, dtype=np.float64)
    cumulative = np.concatenate(([0.0], np.cumsum(np.square(mono))))
    starts = np.arange(0, max(len(mono) - window_samples + 1, 1), hop_samples)
    ends = np.minimum(starts + window_samples, len(mono))
    return np.sqrt((cumulative[ends] - cumulative[starts]) / np.maximum(ends - starts, 1))


def envelope_rise_carrier(
    signal: np.ndarray,
    hop_samples: int,
    window_samples: int = ENVELOPE_WINDOW_SAMPLES,
    label: str = "signal",
) -> np.ndarray:
    """Half-wave-rectified envelope RISE (amendments 6 and 7).

    Amendment 6: a plain amplitude envelope flatlines on sustained material and
    slides to the search bound. The RISE is what carries alignment information
    there. Amendment 7: this is the ONLY carrier alignment may correlate on --
    raw waveform cycle-skips on quasi-periodic material and reports 0.9995
    confidence while doing it.
    """
    envelope = envelope_rms(signal, hop_samples, window_samples)
    if envelope.size < 2:
        raise DegenerateCarrierError(f"{label}: signal too short for an envelope carrier")
    rise = np.maximum(np.diff(envelope), 0.0)
    level = float(np.mean(envelope))
    if level <= 0.0 or float(np.std(rise)) / level < CARRIER_ACTIVITY_FLOOR:
        raise DegenerateCarrierError(
            f"{label}: envelope-rise carrier is flat (std/level "
            f"{(float(np.std(rise)) / level if level > 0 else 0.0):.3e} < {CARRIER_ACTIVITY_FLOOR:.0e}); "
            "an unmodulated tone carries no alignment information"
        )
    return rise


def normalised_cross_correlation(a: np.ndarray, b: np.ndarray, lags: np.ndarray) -> np.ndarray:
    """NCC of ``a`` against ``b`` at each integer lag, normalised per overlap.

    A positive lag means ``b`` is LATE relative to ``a``: ``b[n + lag]``
    lines up with ``a[n]``.
    """
    scores = np.zeros(len(lags), dtype=np.float64)
    for index, lag in enumerate(lags):
        if lag >= 0:
            left, right = a[: len(a) - lag], b[lag:]
        else:
            left, right = a[-lag:], b[: len(b) + lag]
        size = min(len(left), len(right))
        if size < 8:
            continue
        left, right = left[:size], right[:size]
        left = left - left.mean()
        right = right - right.mean()
        denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
        if denominator > 0.0:
            scores[index] = float(np.dot(left, right)) / denominator
    return scores


@dataclass(frozen=True)
class Alignment:
    """The measured integer delay and the evidence it is trustworthy.

    ``runner_up_delay_samples`` exists because a bound check alone cannot prove
    a trim is right: on 16th-note material at 103-158 BPM a 120 ms-early render
    aliases onto the next musical period and reports a near-zero lag. Carrying
    the runner-up peak makes that alias visible in the row instead of absorbed
    by it.
    """

    delay_samples: int
    correlation: float
    peak_ratio: float
    runner_up_delay_samples: int
    carrier: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _guarded_peak(
    scores: np.ndarray, lags: np.ndarray, guard_lags: int, label: str
) -> tuple[int, float, float, int]:
    best_index = int(np.argmax(scores))
    best_score = float(scores[best_index])
    if best_score < ALIGN_CORRELATION_FLOOR:
        raise AlignmentCorrelationFloorError(
            f"{label}: best align correlation {best_score:.4f} is below the floor "
            f"{ALIGN_CORRELATION_FLOOR}; independent signals 'align' at ~0.005, so this "
            "is a refusal, not a measurement"
        )
    masked = scores.copy()
    low = max(best_index - guard_lags, 0)
    high = min(best_index + guard_lags + 1, len(scores))
    masked[low:high] = -np.inf
    if np.any(np.isfinite(masked)):
        runner_index = int(np.argmax(masked))
        runner_up, runner_lag = float(masked[runner_index]), int(lags[runner_index])
    else:
        runner_up, runner_lag = 0.0, 0
    peak_ratio = max(runner_up, 0.0) / best_score if best_score > 0.0 else 1.0
    if peak_ratio > ALIGN_AMBIGUITY_MAX_RATIO:
        raise AlignmentAmbiguousError(
            f"{label}: runner-up correlation {runner_up:.4f} at lag {runner_lag} is "
            f"indistinguishable from the best {best_score:.4f} at lag {int(lags[best_index])} "
            f"(ratio {peak_ratio:.3f} > {ALIGN_AMBIGUITY_MAX_RATIO}); the correlation does "
            "not identify a delay"
        )
    return int(lags[best_index]), best_score, peak_ratio, runner_lag


def estimate_delay_samples(
    reference: np.ndarray,
    rendered: np.ndarray,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    label: str = "row",
) -> Alignment:
    """Two-stage integer-delay alignment on the envelope-rise carrier.

    Positive ``delay_samples`` means ``rendered`` is late by that many samples.
    Both stages correlate the half-wave-rectified envelope rise; raw waveform
    is never correlated anywhere (amendment 7).
    """
    ref_mono, test_mono = to_mono(reference), to_mono(rendered)

    coarse_hop = ONSET_HOP_SAMPLES
    ref_coarse = envelope_rise_carrier(ref_mono, coarse_hop, label=f"{label} reference")
    test_coarse = envelope_rise_carrier(test_mono, coarse_hop, label=f"{label} render")
    max_lag_frames = int(round(ALIGN_COARSE_MAX_LAG_MS * sample_rate_hz / 1000.0 / coarse_hop))
    coarse_lags = np.arange(-max_lag_frames, max_lag_frames + 1)
    coarse_scores = normalised_cross_correlation(ref_coarse, test_coarse, coarse_lags)
    guard_frames = max(int(round(ALIGN_AMBIGUITY_GUARD_MS * sample_rate_hz / 1000.0 / coarse_hop)), 1)
    coarse_lag, _, coarse_ratio, coarse_runner = _guarded_peak(
        coarse_scores, coarse_lags, guard_frames, f"{label} coarse"
    )

    fine_hop = ALIGN_FINE_HOP_SAMPLES
    ref_fine = envelope_rise_carrier(ref_mono, fine_hop, label=f"{label} reference fine")
    test_fine = envelope_rise_carrier(test_mono, fine_hop, label=f"{label} render fine")
    centre = int(round(coarse_lag * coarse_hop / fine_hop))
    span = int(math.ceil(coarse_hop / fine_hop)) * 2
    fine_lags = np.arange(centre - span, centre + span + 1)
    fine_scores = normalised_cross_correlation(ref_fine, test_fine, fine_lags)
    fine_index = int(np.argmax(fine_scores))
    if fine_index in (0, len(fine_lags) - 1):
        raise AlignmentBoundError(
            f"{label}: fine-stage argmax landed on its search bound "
            f"(lag {int(fine_lags[fine_index]) * fine_hop} samples, window "
            f"+-{span * fine_hop} samples around the coarse estimate); the coarse "
            "stage and the fine stage disagree"
        )
    fine_guard = max(int(round(ALIGN_AMBIGUITY_GUARD_MS * sample_rate_hz / 1000.0 / fine_hop)), 1)
    fine_lag, correlation, fine_ratio, _ = _guarded_peak(
        fine_scores, fine_lags, fine_guard, f"{label} fine"
    )
    return Alignment(
        delay_samples=int(fine_lag * fine_hop),
        correlation=correlation,
        peak_ratio=max(coarse_ratio, fine_ratio),
        runner_up_delay_samples=int(coarse_runner * coarse_hop),
        carrier="half-wave-rectified envelope rise",
    )


def apply_delay(
    reference: np.ndarray, rendered: np.ndarray, delay_samples: int
) -> tuple[np.ndarray, np.ndarray]:
    """Trim both signals to their aligned, common span."""
    ref_mono, test_mono = to_mono(reference), to_mono(rendered)
    if delay_samples >= 0:
        test_mono = test_mono[delay_samples:]
    else:
        ref_mono = ref_mono[-delay_samples:]
    size = min(len(ref_mono), len(test_mono))
    if size <= 0:
        raise AlignmentError(f"alignment by {delay_samples} samples leaves no overlap")
    return ref_mono[:size], test_mono[:size]


# ----- spectra --------------------------------------------------------------


def stft_magnitude(signal: np.ndarray, fft_samples: int, hop_samples: int) -> np.ndarray:
    """Hann-windowed magnitude STFT, frames x bins."""
    mono = np.asarray(signal, dtype=np.float64)
    if len(mono) < fft_samples:
        raise ValueError(f"signal of {len(mono)} samples is shorter than the {fft_samples} FFT")
    window = np.hanning(fft_samples)
    starts = np.arange(0, len(mono) - fft_samples + 1, hop_samples)
    frames = np.stack([mono[start : start + fft_samples] * window for start in starts])
    return np.abs(np.fft.rfft(frames, axis=1))


def assert_low_band_resolution(fft_samples: int, sample_rate_hz: int = SAMPLE_RATE_HZ) -> None:
    """Structural analogue of the librosa mel dead-low-band defect.

    librosa's ``onset_strength`` at ``n_fft=512`` with ``n_mels=128`` silently
    produces dead low-frequency mel bands and swallows the warning, weakening
    bass-led onsets on exactly the bass-heavy fixture. This module uses linear
    bins so that failure mode cannot occur, and asserts the equivalent property
    directly rather than assuming it.
    """
    bin_hz = sample_rate_hz / fft_samples
    if bin_hz > LOW_BAND_MAX_BIN_HZ:
        raise LowBandResolutionError(
            f"FFT of {fft_samples} gives {bin_hz:.1f} Hz bins, coarser than the "
            f"{LOW_BAND_MAX_BIN_HZ} Hz low-band requirement; bass-led onsets would be blunted"
        )
    bins_below_250 = int(250.0 / bin_hz)
    if bins_below_250 < LOW_BAND_MIN_BINS_BELOW_250HZ:
        raise LowBandResolutionError(
            f"only {bins_below_250} bins fall below 250 Hz, need "
            f"{LOW_BAND_MIN_BINS_BELOW_250HZ}"
        )


def log_spectral_distance(
    reference: np.ndarray,
    rendered: np.ndarray,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    frame_ms: float = LSD_FRAME_MS,
) -> float:
    """LSD in dB, floored 80 dB below the REFERENCE PEAK (amendment 4).

    An absolute -100 dBFS floor lets empty-bin numerical noise dominate: the
    same null test reads 39.7 dB absolute-floored and 1.14 dB relative-floored.
    """
    fft_samples = int(round(frame_ms * sample_rate_hz / 1000.0))
    hop_samples = fft_samples // 2
    ref_spectrum = stft_magnitude(reference, fft_samples, hop_samples)
    test_spectrum = stft_magnitude(rendered, fft_samples, hop_samples)
    frames = min(ref_spectrum.shape[0], test_spectrum.shape[0])
    ref_spectrum, test_spectrum = ref_spectrum[:frames], test_spectrum[:frames]
    reference_peak = float(np.max(ref_spectrum))
    if reference_peak <= 0.0:
        raise SilentExcerptError("LSD reference has no spectral energy")
    floor = reference_peak * (10.0 ** (-LSD_FLOOR_DB_BELOW_REF_PEAK / 20.0))
    ref_db = 20.0 * np.log10(np.maximum(ref_spectrum, floor))
    test_db = 20.0 * np.log10(np.maximum(test_spectrum, floor))
    per_frame = np.sqrt(np.mean(np.square(ref_db - test_db), axis=1))
    return float(np.mean(per_frame))


@dataclass(frozen=True)
class Residual:
    """Round-trip residual, a DIAGNOSTIC only -- it never ranks a build."""

    residual_dbr: float
    correlation_rho: float

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def residual_dbr(reference: np.ndarray, rendered: np.ndarray) -> Residual:
    """Least-squares-gain-fitted residual, and the rho it is equivalent to.

    Amendment 1 records the identity ``residual_dBr = 10*log10(1 - rho^2)``;
    both are computed here so the pair cross-checks the implementation.
    """
    ref = np.asarray(reference, dtype=np.float64)
    test = np.asarray(rendered, dtype=np.float64)
    ref_energy = float(np.dot(ref, ref))
    test_energy = float(np.dot(test, test))
    if ref_energy <= 0.0 or test_energy <= 0.0:
        raise SilentExcerptError("residual needs two non-silent signals")
    gain = float(np.dot(ref, test)) / test_energy
    residual = ref - gain * test
    rho = float(np.dot(ref, test)) / math.sqrt(ref_energy * test_energy)
    value = float(np.dot(residual, residual)) / ref_energy
    return Residual(
        residual_dbr=10.0 * math.log10(max(value, 1e-30)),
        correlation_rho=rho,
    )


# ----- onsets ---------------------------------------------------------------


def assert_onset_hop_resolution(
    hop_samples: int,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    gate_ms: float = ONSET_RECOVERY_GATE_MS,
) -> float:
    """Amendment 5: raise when hop resolution exceeds gate/4.

    librosa's default 512 hop is 11.61 ms at 44.1 kHz, ONE quantisation step
    below a 12 ms gate, so a displacement measured with it cannot distinguish
    "inside the gate" from "one step outside it".
    """
    resolution_ms = hop_samples / sample_rate_hz * 1000.0
    limit_ms = gate_ms / 4.0
    if resolution_ms > limit_ms:
        raise HopResolutionError(
            f"onset hop {hop_samples} samples = {resolution_ms:.3f} ms exceeds gate/4 "
            f"= {limit_ms:.3f} ms for a {gate_ms} ms gate"
        )
    return resolution_ms


def onset_strength(
    signal: np.ndarray,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    hop_samples: int = ONSET_HOP_SAMPLES,
    fft_samples: int = ONSET_FLUX_FFT_SAMPLES,
) -> np.ndarray:
    """Half-wave-rectified spectral flux (amendment 6), normalised to unit max."""
    assert_onset_hop_resolution(hop_samples, sample_rate_hz)
    assert_low_band_resolution(fft_samples, sample_rate_hz)
    spectrum = stft_magnitude(signal, fft_samples, hop_samples)
    flux = np.sum(np.maximum(np.diff(spectrum, axis=0), 0.0), axis=1)
    peak = float(np.max(flux)) if flux.size else 0.0
    return flux / peak if peak > 0.0 else flux


def pick_onsets(
    strength: np.ndarray,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    hop_samples: int = ONSET_HOP_SAMPLES,
) -> np.ndarray:
    """Onset times in seconds, picked off the rectified flux."""
    if strength.size == 0:
        return np.zeros(0)
    frame_ms = hop_samples / sample_rate_hz * 1000.0
    median_frames = max(int(round(ONSET_PEAK_MEDIAN_WINDOW_MS / frame_ms)), 1)
    padded = np.pad(strength, median_frames, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * median_frames + 1)
    threshold = np.median(windows, axis=1)[: len(strength)] * ONSET_PEAK_FACTOR + ONSET_PEAK_DELTA
    candidates = np.flatnonzero(
        (strength[1:-1] > strength[:-2]) & (strength[1:-1] >= strength[2:]) & (strength[1:-1] > threshold[1:-1])
    ) + 1
    separation_frames = max(int(round(ONSET_MIN_SEPARATION_MS / frame_ms)), 1)
    kept: list[int] = []
    for frame in candidates[np.argsort(-strength[candidates])]:
        if all(abs(frame - chosen) >= separation_frames for chosen in kept):
            kept.append(int(frame))
    return np.sort(np.asarray(kept, dtype=np.float64)) * hop_samples / sample_rate_hz


@dataclass(frozen=True)
class OnsetComparison:
    """Onset displacement and recovery -- two of the three RANKING metrics."""

    reference_onsets: int
    matched_onsets: int
    displacement_p95_ms: float
    displacement_median_ms: float
    recovery_fraction: float
    recovery_gate_ms: float

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def compare_onsets(
    reference: np.ndarray,
    rendered: np.ndarray,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    hop_samples: int = ONSET_HOP_SAMPLES,
    gate_ms: float = ONSET_RECOVERY_GATE_MS,
) -> OnsetComparison:
    """Match each reference onset to its nearest render onset."""
    assert_onset_hop_resolution(hop_samples, sample_rate_hz, gate_ms)
    ref_onsets = pick_onsets(onset_strength(reference, sample_rate_hz, hop_samples), sample_rate_hz, hop_samples)
    test_onsets = pick_onsets(onset_strength(rendered, sample_rate_hz, hop_samples), sample_rate_hz, hop_samples)
    if ref_onsets.size == 0:
        raise HarnessIntegrityError(
            "no onsets found in the reference; displacement and recovery are undefined"
        )
    displacements: list[float] = []
    for onset in ref_onsets:
        if test_onsets.size == 0:
            continue
        nearest = float(test_onsets[int(np.argmin(np.abs(test_onsets - onset)))])
        delta_ms = (nearest - onset) * 1000.0
        if abs(delta_ms) <= ONSET_MATCH_WINDOW_MS:
            displacements.append(delta_ms)
    absolute = np.abs(np.asarray(displacements)) if displacements else np.zeros(0)
    recovered = int(np.sum(absolute <= gate_ms)) if absolute.size else 0
    return OnsetComparison(
        reference_onsets=int(ref_onsets.size),
        matched_onsets=int(absolute.size),
        displacement_p95_ms=float(np.percentile(absolute, 95)) if absolute.size else float("inf"),
        displacement_median_ms=float(np.median(absolute)) if absolute.size else float("inf"),
        recovery_fraction=recovered / float(ref_onsets.size),
        recovery_gate_ms=gate_ms,
    )
