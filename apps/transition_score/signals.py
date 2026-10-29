"""Signal primitives the scorer measures with (TRYER-01).

Every function takes float audio shaped ``(frames, channels)`` (or mono
``(frames,)``) at a given sample rate and returns plain numbers or arrays. No
function here decides anything: thresholds live in :mod:`scorer`.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import fft, signal

#: Low band of the kick and bass onset envelope (spec 10 section 3: 40-150 Hz).
LOW_BAND_HZ = (40.0, 150.0)
#: Envelope hop: 64 samples is 1.33 ms at 48 kHz (spec 10: hop 64).
ENVELOPE_HOP = 64
#: Smoothing of the rectified low band before differencing, in seconds.
ENVELOPE_SMOOTH_S = 0.004
#: Hops an onset rise is measured over (5 ms at 48 kHz): an attack, not a swell.
ONSET_SPAN = 4
#: BS.1770 short-term and momentary windows.
SHORT_TERM_S = 3.0
MOMENTARY_S = 0.4
LOUDNESS_HOP_S = 0.1
#: True peak oversampling (BS.1770-4 annex 2: 4x at 48 kHz).
TRUE_PEAK_OVERSAMPLE = 4
_SILENCE_FLOOR = 1e-12


def mono(audio: np.ndarray) -> np.ndarray:
    """Average the channels (a mono array passes through)."""
    return audio if audio.ndim == 1 else audio.mean(axis=1)


def analytic_magnitude(x: np.ndarray) -> np.ndarray:
    """Hilbert magnitude, FFT padded to a fast length (an awkward length can take seconds)."""
    return np.abs(signal.hilbert(x, N=fft.next_fast_len(len(x))))[: len(x)]


def band_signal(audio: np.ndarray, sr: int, band_hz: tuple[float, float]) -> np.ndarray:
    """The mono signal in ``band_hz``: a zero-phase 4th-order Butterworth band pass."""
    sos = signal.butter(4, band_hz, btype="bandpass", fs=sr, output="sos")
    return signal.sosfiltfilt(sos, mono(audio).astype(np.float64))


def span_power_db(x: np.ndarray, sr: int, edges: list[tuple[float, float]]) -> np.ndarray:
    """Mean power of ``x`` over each (start, end) span in seconds, in dB: levels that add as power, so two decks'
    lows at half power each read as one deck at full (an amplitude envelope would read their sum about 1 dB low)."""
    return np.array(
        [10.0 * math.log10(max(float(np.mean(x[int(a * sr) : int(b * sr)] ** 2)), _SILENCE_FLOOR)) for a, b in edges]
    )


def band_envelope(audio: np.ndarray, sr: int, band_hz: tuple[float, float] = LOW_BAND_HZ) -> np.ndarray:
    """The band's amplitude envelope at ``ENVELOPE_HOP`` resolution.

    Hilbert magnitude, not squared energy: a 50 Hz kick squared ripples at
    100 Hz, and that ripple would read as onsets. Zero-phase filters, so both
    decks are delayed alike (not at all).
    """
    magnitude = analytic_magnitude(band_signal(audio, sr, band_hz))
    smooth = signal.butter(2, 1.0 / ENVELOPE_SMOOTH_S, btype="lowpass", fs=sr, output="sos")
    envelope = signal.sosfiltfilt(smooth, magnitude)
    frames = len(envelope) // ENVELOPE_HOP
    return np.maximum(envelope[: frames * ENVELOPE_HOP].reshape(frames, ENVELOPE_HOP).mean(axis=1), 0.0)


def onset_envelope(envelope: np.ndarray) -> np.ndarray:
    """Onset strength from a band envelope (:func:`band_envelope`): its squared rise over ``ONSET_SPAN`` hops.

    Linear, not log: in log a decaying kick that dips into the null of two
    low notes beating rises steeply out of it and reads as an onset. A
    correlation peak is the same at any level, so a quiet deck times as well.
    """
    env = envelope
    rise = np.maximum(env[ONSET_SPAN:] - env[:-ONSET_SPAN], 0.0)
    # Squared: a kick's attack rises within a few ms and dominates; a slow
    # swell (two low notes beating) rises a little per hop and fades out.
    return np.concatenate([np.zeros(ONSET_SPAN), rise * rise])


def level_db(audio: np.ndarray, sr: int, frame_s: float) -> np.ndarray:
    """Amplitude envelope (Hilbert magnitude) per frame, in dBFS: no waveform ripple, so a step reads where it is."""
    magnitude = analytic_magnitude(mono(audio).astype(np.float64))
    size = max(1, round(frame_s * sr))
    frames = len(magnitude) // size
    mean = magnitude[: frames * size].reshape(frames, size).mean(axis=1)
    return 20.0 * np.log10(np.maximum(mean, 1e-9))


def _biquad(kind: str, fc: float, gain_db: float, q: float, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """RBJ biquads with BS.1770's K-weighting constants (the pyloudnorm formulation)."""
    a_gain = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * fc / sr
    alpha = math.sin(w0) / (2.0 * q)
    cos_w0 = math.cos(w0)
    if kind == "high_shelf":
        root = 2.0 * math.sqrt(a_gain) * alpha
        b = [
            a_gain * ((a_gain + 1) + (a_gain - 1) * cos_w0 + root),
            -2 * a_gain * ((a_gain - 1) + (a_gain + 1) * cos_w0),
            a_gain * ((a_gain + 1) + (a_gain - 1) * cos_w0 - root),
        ]
        a = [
            (a_gain + 1) - (a_gain - 1) * cos_w0 + root,
            2 * ((a_gain - 1) - (a_gain + 1) * cos_w0),
            (a_gain + 1) - (a_gain - 1) * cos_w0 - root,
        ]
    elif kind == "high_pass":
        b = [(1 + cos_w0) / 2, -(1 + cos_w0), (1 + cos_w0) / 2]
        a = [1 + alpha, -2 * cos_w0, 1 - alpha]
    else:
        raise ValueError(f"unknown biquad {kind!r}")
    return np.array(b) / a[0], np.array(a) / a[0]


def k_weighted(audio: np.ndarray, sr: int) -> np.ndarray:
    """BS.1770 K-weighting: the head shelf then the RLB high pass, per channel."""
    x = audio[:, None] if audio.ndim == 1 else audio
    b1, a1 = _biquad("high_shelf", 1681.974450955533, 3.999843853973347, 0.7071752369554196, sr)
    b2, a2 = _biquad("high_pass", 38.13547087602444, 0.0, 0.5003270373238773, sr)
    return signal.lfilter(b2, a2, signal.lfilter(b1, a1, x.astype(np.float64), axis=0), axis=0)


def loudness_series(
    audio: np.ndarray, sr: int, window_s: float, hop_s: float = LOUDNESS_HOP_S
) -> tuple[np.ndarray, np.ndarray]:
    """(centers_s, LUFS) of BS.1770 loudness over sliding windows (no gating: a dropout must read low)."""
    weighted = k_weighted(audio, sr)
    power = (weighted * weighted).sum(axis=1)
    window, hop = round(window_s * sr), round(hop_s * sr)
    if len(power) < window:
        return np.zeros(0), np.zeros(0)
    cumulative = np.concatenate([[0.0], np.cumsum(power)])
    starts = np.arange(0, len(power) - window + 1, hop)
    mean_power = (cumulative[starts + window] - cumulative[starts]) / window
    lufs = -0.691 + 10.0 * np.log10(np.maximum(mean_power, _SILENCE_FLOOR))
    return (starts + window / 2.0) / sr, lufs


def true_peak_dbtp(audio: np.ndarray) -> float:
    """BS.1770 true peak: the 4x oversampled absolute maximum, in dBTP."""
    x = audio[:, None] if audio.ndim == 1 else audio
    if len(x) == 0:
        raise ValueError("true peak of no audio")
    up = signal.resample_poly(x.astype(np.float64), TRUE_PEAK_OVERSAMPLE, 1, axis=0)
    peak = max(float(np.abs(up).max()), float(np.abs(x).max()))
    return 20.0 * math.log10(max(peak, _SILENCE_FLOOR))


def rms_db(audio: np.ndarray) -> float:
    x = mono(audio)
    return 10.0 * math.log10(max(float(np.mean(x.astype(np.float64) ** 2)), _SILENCE_FLOOR))


def frame_rms_db(audio: np.ndarray, sr: int, frame_s: float) -> np.ndarray:
    """RMS level per non-overlapping frame, in dBFS."""
    x = mono(audio).astype(np.float64)
    size = max(1, round(frame_s * sr))
    frames = len(x) // size
    power = (x[: frames * size].reshape(frames, size) ** 2).mean(axis=1)
    return 10.0 * np.log10(np.maximum(power, _SILENCE_FLOOR))


def click_ratio(
    audio: np.ndarray, sr: int, center_s: float, half_window_s: float = 0.02, reference_s: float = 0.1
) -> float:
    """How far the sharpest second difference near ``center_s`` stands above its surroundings.

    A click is a discontinuity: a one-sample jump in level or slope that no
    instrument makes. Its second difference towers over the median of the
    second difference around it; a hi-hat or a kick does not. Robust scale:
    1.4826 * median absolute deviation.
    """
    x = mono(audio).astype(np.float64)
    d2 = np.abs(np.diff(x, n=2))
    center = round(center_s * sr)
    lo, hi = max(0, center - int(half_window_s * sr)), min(len(d2), center + int(half_window_s * sr))
    ref_lo, ref_hi = max(0, center - int(reference_s * sr)), min(len(d2), center + int(reference_s * sr))
    if hi <= lo or ref_hi - ref_lo < 16:
        raise ValueError(f"no audio around {center_s:.3f} s to look for a click in")
    scale = 1.4826 * float(np.median(d2[ref_lo:ref_hi])) + 1e-9
    return float(d2[lo:hi].max()) / scale
