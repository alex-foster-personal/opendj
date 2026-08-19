"""Synthetic material whose shape breaks a naive measurement.

This is a CATALOGUE, not test scaffolding: each generator here corresponds to a
documented way the instrument can be wrong, so it is worth being able to reach
for one from a calibration session as easily as from a test.

===========================  ===============================================
``pure_sine``                raw correlation returns lag 100 for a true 300
                             at corr 0.9995, and a rectified-envelope carrier
                             alone still cycle-skips it at 0.999
``crescendo``                transient-free: raw correlation returns -101 at
                             corr 0.9996
``independent_noise``        "aligns" at corr ~0.005 if nothing floors it
``click_train``              16th-note periodicity: a one-period displacement
                             is genuinely indistinguishable from none
``aperiodic_bursts``         random spacing, so nothing aliases onto a period
``percussive``              broadband and irregular: the positive control
``low_band_kick``            60 Hz with a raised-cosine attack, so a detector
                             with dead low mel bands cannot fake finding it
``harmonic_tone``            f0 trackers meet harmonics, never a bare sine
===========================  ===============================================

Deterministic by construction: every generator seeds its own RNG, so a probe
that fails once fails every time.
"""

from __future__ import annotations

import math

import numpy as np

from scripts.quality.contract import SAMPLE_RATE_HZ

PROBE_SEED = 20260819


def rng(seed: int = PROBE_SEED) -> np.random.Generator:
    return np.random.default_rng(seed)


def delay(signal: np.ndarray, samples: int) -> np.ndarray:
    """Return ``signal`` delayed by ``samples``, same length."""
    return np.concatenate([np.zeros(samples), signal])[: len(signal)]


def shift(signal: np.ndarray, samples: int) -> np.ndarray:
    """Signed shift: positive is LATE, negative is EARLY. Same length."""
    if samples >= 0:
        return delay(signal, samples)
    return np.concatenate([signal[-samples:], np.zeros(-samples)])


def percussive(seconds: float = 4.0, hits_per_second: float = 4.0) -> np.ndarray:
    """Noise-burst train: broadband, transient-rich, unambiguously alignable."""
    generator = rng()
    length = int(seconds * SAMPLE_RATE_HZ)
    signal = generator.standard_normal(length) * 0.02
    step = int(SAMPLE_RATE_HZ / hits_per_second)
    for index, start in enumerate(range(0, length - step, step)):
        # Irregular spacing so the train is not itself quasi-periodic.
        offset = start + (index * 137) % 400
        burst = int(0.05 * SAMPLE_RATE_HZ)
        envelope = np.exp(-np.linspace(0, 12, burst))
        signal[offset : offset + burst] += generator.standard_normal(burst) * envelope
    return signal / np.max(np.abs(signal))


def aperiodic_bursts(seconds: float = 8.0) -> np.ndarray:
    """Burst train with random spacing, so nothing aliases onto a musical period."""
    generator = rng(4242)
    length = int(seconds * SAMPLE_RATE_HZ)
    signal = generator.standard_normal(length) * 0.02
    burst = int(0.05 * SAMPLE_RATE_HZ)
    envelope = np.exp(-np.linspace(0, 12, burst))
    position = 0.2
    while position < seconds - 0.3:
        start = int(position * SAMPLE_RATE_HZ)
        signal[start : start + burst] += generator.standard_normal(burst) * envelope
        position += float(generator.uniform(0.15, 0.6))
    return signal / np.max(np.abs(signal))


def click_train(period_samples: int, seconds: float = 4.0) -> np.ndarray:
    """Perfectly periodic clicks: the aliasing trap, at whatever period is asked for."""
    length = int(seconds * SAMPLE_RATE_HZ)
    signal = np.zeros(length)
    burst = 200
    envelope = np.exp(-np.linspace(0, 8, burst))
    for start in range(0, length - burst, period_samples):
        signal[start : start + burst] += envelope
    return signal


def pure_sine(hz: float = 1000.0, seconds: float = 2.0) -> np.ndarray:
    return np.sin(2 * math.pi * hz * np.arange(int(seconds * SAMPLE_RATE_HZ)) / SAMPLE_RATE_HZ)


def crescendo(hz: float = 440.0, seconds: float = 3.0) -> np.ndarray:
    """A tone under a slow linear swell: no transient anywhere to align on."""
    samples = int(seconds * SAMPLE_RATE_HZ)
    return pure_sine(hz, seconds) * np.linspace(0.001, 1.0, samples)


def low_band_kick(
    hit_times: tuple[float, ...] = (0.5, 1.5, 2.5, 3.5),
    seconds: float = 4.0,
    hz: float = 60.0,
) -> np.ndarray:
    """Kicks with essentially no energy above 250 Hz.

    The raised-cosine attack is what makes this a low-frequency KICK rather than
    a broadband click with a low tone underneath it. Without it the step
    discontinuity is broadband and a detector deaf to the low band would still
    "find" the hits, which would make the probe prove nothing.
    """
    length = int(seconds * SAMPLE_RATE_HZ)
    signal = np.zeros(length)
    burst, attack = int(0.12 * SAMPLE_RATE_HZ), int(0.006 * SAMPLE_RATE_HZ)
    envelope = np.exp(-np.linspace(0, 10, burst))
    envelope[:attack] *= 0.5 * (1 - np.cos(np.pi * np.arange(attack) / attack))
    tone = np.sin(2 * math.pi * hz * np.arange(burst) / SAMPLE_RATE_HZ)
    for hit in hit_times:
        start = int(hit * SAMPLE_RATE_HZ)
        signal[start : start + burst] += tone * envelope
    return signal


def harmonic_tone(hz: float, seconds: float = 3.0, harmonics: int = 5) -> np.ndarray:
    """A harmonic-rich pitched tone -- a bare sine is not what f0 trackers meet."""
    time = np.arange(int(seconds * SAMPLE_RATE_HZ)) / SAMPLE_RATE_HZ
    signal = sum(np.sin(2 * math.pi * hz * n * time) / n for n in range(1, harmonics + 1))
    return signal / np.max(np.abs(signal))


def cents_to_ratio(cents: float) -> float:
    return 2.0 ** (cents / 1200.0)
