"""GRIDFIT-01: judge a beatgrid against its own audio ((private path)).

Real music cannot be committed, so every case is a synthetic track with a known
tempo: a kick (decaying 55 Hz thump plus a short click) on every beat, a closed
hi-hat AND a bass note on every off-beat. The off-beat bass is deliberate: it
is what made the signal-only off-beat rule fail on real house music (round A1),
so a rule that confuses it with a kick must fail here too.

Regression lines:
- if a grid at the audio's tempo is not `fit` then every good track nags
- if a 127.00 grid on 128 BPM audio is not `wrong_bpm` with ~128.0 then the
  Don't Stop case ships again
- if a half-tempo grid is `wrong_bpm` then octave grids are misreported
- if a grid moved 50 ms is not `shifted` (and an unmoved one is) then the
  shift verdict is noise
- if noise or a kickless pad gets any verdict but `no_verdict` then the check
  invents findings where it cannot measure
- if off-beat is decided without activations then the A1 failure is back
"""

from __future__ import annotations

import numpy as np
import pytest

from apps.analysis_beatgrid.grid_audio_fit import (
    THRESHOLDS,
    GridBeat,
    judge_grid_against_audio,
    rule_version,
)

SR = 22050


def _track(bpm: float, seconds: float = 90.0, *, kicks: bool = True, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(SR * seconds)
    out = np.zeros(n, dtype=np.float32)
    period = 60.0 / bpm
    t_kick = np.arange(int(0.25 * SR)) / SR
    kick = (np.sin(2 * np.pi * 55 * t_kick) * np.exp(-t_kick * 18)).astype(np.float32)
    kick[:220] += rng.normal(0, 0.4, 220).astype(np.float32) * np.exp(-np.arange(220) / 40)
    t_hat = np.arange(int(0.04 * SR)) / SR
    hat = (rng.normal(0, 0.15, len(t_hat)) * np.exp(-t_hat * 120)).astype(np.float32)
    t_bass = np.arange(int(0.18 * SR)) / SR
    bass = (0.6 * np.sin(2 * np.pi * 70 * t_bass) * np.minimum(1, t_bass * 200) * np.exp(-t_bass * 9)).astype(
        np.float32
    )
    k = 0
    while (t := 0.5 + k * period) < seconds - 1:
        i = int(t * SR)
        if kicks:
            out[i : i + len(kick)] += kick
        j = int((t + period / 2) * SR)
        out[j : j + len(hat)] += hat
        out[j : j + len(bass)] += bass
        k += 1
    out += rng.normal(0, 0.003, n).astype(np.float32)
    return out


def _grid(bpm: float, seconds: float = 90.0, *, first: float = 0.5, marker_bpm: float | None = None) -> list[GridBeat]:
    period = 60.0 / bpm
    return [
        GridBeat(t=round(first + k * period, 3), bpm=marker_bpm or bpm)
        for k in range(int((seconds - first - 1) / period))
    ]


def test_a_grid_at_the_audio_tempo_fits():
    fit = judge_grid_against_audio(_track(124.0), SR, _grid(124.0))
    assert fit.verdict == "fit", fit
    assert abs(fit.audio_bpm - 124.0) < 0.02


def test_a_one_bpm_slow_grid_is_wrong_bpm_and_names_the_audio_tempo():
    fit = judge_grid_against_audio(_track(128.0), SR, _grid(127.0))
    assert fit.verdict == "wrong_bpm", fit
    assert fit.grid_bpm == pytest.approx(127.0)
    assert fit.audio_bpm == pytest.approx(128.0, abs=0.02)


@pytest.mark.parametrize("error_bpm", [0.15, -0.3])
def test_small_tempo_errors_above_the_threshold_are_caught(error_bpm):
    fit = judge_grid_against_audio(_track(125.0), SR, _grid(125.0 + error_bpm))
    assert fit.verdict == "wrong_bpm", fit


def test_an_error_below_the_threshold_is_not_reported():
    fit = judge_grid_against_audio(_track(125.0), SR, _grid(125.05))
    assert fit.verdict == "fit", fit


def test_a_half_tempo_grid_is_not_wrong_bpm():
    fit = judge_grid_against_audio(_track(128.0), SR, _grid(64.0))
    assert fit.verdict != "wrong_bpm", fit


def test_a_grid_moved_50_ms_is_shifted_and_says_by_how_much():
    audio = _track(122.0)
    unmoved = judge_grid_against_audio(audio, SR, _grid(122.0))
    moved = judge_grid_against_audio(audio, SR, _grid(122.0, first=0.55))
    assert unmoved.verdict == "fit", unmoved
    assert moved.verdict == "shifted", moved
    assert moved.kick_offset_ms - unmoved.kick_offset_ms == pytest.approx(-50, abs=6)


def test_noise_and_a_kickless_track_get_no_verdict():
    rng = np.random.default_rng(3)
    noise = rng.normal(0, 0.2, SR * 60).astype(np.float32)
    assert judge_grid_against_audio(noise, SR, _grid(120.0, 60.0)).verdict == "no_verdict"
    pads = _track(120.0, kicks=False)
    assert judge_grid_against_audio(pads, SR, _grid(120.0)).verdict == "no_verdict"


def _logits(bpm: float, seconds: float = 90.0, fps: int = 50) -> np.ndarray:
    """Beat This!-shaped framewise logits: high on the beats, low elsewhere."""
    logits = np.full(int(seconds * fps), -6.0)
    period = 60.0 / bpm
    for k in range(int((seconds - 1) / period)):
        logits[int(np.rint((0.5 + k * period) * fps))] = 4.0
    return logits


def test_off_beat_needs_the_model_and_uses_it():
    audio = _track(126.0)
    offbeat_grid = _grid(126.0, first=0.5 + 60.0 / 126.0 / 2)
    without = judge_grid_against_audio(audio, SR, offbeat_grid)
    assert without.verdict != "off_beat", "off-beat must never be decided from the signal alone (round A2)"
    with_model = judge_grid_against_audio(audio, SR, offbeat_grid, beat_logits=_logits(126.0), logits_fps=50)
    assert with_model.verdict == "off_beat", with_model
    on_grid = judge_grid_against_audio(audio, SR, _grid(126.0), beat_logits=_logits(126.0), logits_fps=50)
    assert on_grid.verdict == "fit", on_grid


def test_the_grid_tempo_is_the_stored_marker_not_the_rounded_gaps():
    # Beat times rounded to 1 ms make the median gap 0.04 BPM off at 126; the
    # marker says 126.00 and that is what must be compared (round A0).
    fit = judge_grid_against_audio(_track(126.0), SR, _grid(126.0))
    assert fit.grid_bpm == 126.0


def test_rule_version_tracks_the_thresholds():
    assert rule_version().startswith("grid-audio-fit-1:")
    assert rule_version(THRESHOLDS) == rule_version()
    from dataclasses import replace

    assert rule_version(replace(THRESHOLDS, min_bpm_error=0.2)) != rule_version()
