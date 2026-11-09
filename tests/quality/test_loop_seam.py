"""The instrument behind the loop seam spike numbers (scripts.quality.loop_seam, issue #9843).

Synthetic numpy audio only: no fixtures, no browser.

Regression lines:
  - if a hard splice of a sine cut mid-cycle does not read over 1.5 then the splice control is broken
  - if a continuous sine reads 1.5 or more at the same points then the click instrument is broken
  - if a PCM file that no longer matches its manifest sha256 loads then stale renders get measured
  - if a source delayed by 117 frames is not found at 117 +- 1 then the offset finder is broken
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from apps.transition_score.signals import click_ratio
from scripts.quality.loop_seam import (
    find_output_offset_frames,
    load_verified_pcm,
    splice_join_ratios,
)

SR = 44100


def _sine(seconds: float, hz: float = 440.0) -> np.ndarray:
    return np.sin(2 * np.pi * hz * np.arange(round(seconds * SR)) / SR)


def test_splice_of_a_sine_cut_mid_cycle_reads_as_a_click() -> None:
    source = _sine(1.0)
    length = round(0.1 * SR) + 37  # not a whole number of cycles: every join jumps
    ratios = splice_join_ratios(source, 0, length, 20, SR)
    assert ratios, "no joins measured"
    assert float(np.median(ratios)) > 1.5, "if the splice median is not over 1.5 the splice control is broken"


def test_continuous_sine_does_not_read_as_a_click() -> None:
    source = _sine(2.0)
    length = round(0.1 * SR) + 37
    ratios = [click_ratio(source, SR, k * length / SR) for k in range(2, 15)]
    assert max(ratios) < 1.5, "if a continuous sine reads 1.5 or more the click instrument is broken"


def test_pcm_with_a_stale_sha256_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "render.f32"
    pcm = np.zeros((64, 2), dtype="<f4")
    path.write_bytes(pcm.tobytes())
    good = hashlib.sha256(path.read_bytes()).hexdigest()
    assert load_verified_pcm(path, good, 2).shape == (64, 2)
    with pytest.raises(ValueError, match="sha256"):
        load_verified_pcm(path, "0" * 64, 2)


def test_offset_finder_recovers_a_known_delay() -> None:
    rng = np.random.default_rng(9843)
    source = rng.standard_normal(SR * 2)
    delay = 117
    start = SR // 2
    output = np.concatenate([np.zeros(delay), source[start:]])[:SR]
    lag, peak = find_output_offset_frames(output, source, start, (4410, 39690), 13230)
    assert abs(lag - delay) <= 1, f"if the delay {delay} reads as {lag} the offset finder is broken"
    assert peak > 0.99
