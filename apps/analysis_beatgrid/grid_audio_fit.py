"""Judge a beatgrid against its own audio (GRIDFIT-01).

The shape rule (`grid_quality.py`, GRIDFLAG-01) says whether a grid is evenly
spaced and says outright that it cannot tell whether the grid matches the
music. This module is the other half: it listens. Method, thresholds and the
rounds that set them: `(private path)`; decision:
`(private path)`.

Pipeline, pure and deterministic (PCM in, verdict out, no I/O):

1. Kick-band onset envelope: magnitude spectrum 40 to 140 Hz over a 46 ms
   window every 5 ms, positive log-flux. Computed in chunks so a long track
   never holds a whole-track spectrum (round B0 measured 1.5 GB doing that).
2. Audio tempo: fold the envelope at candidate beat periods around the grid's
   tempo; the sharpest fold is the audio's constant tempo (about 0.005 BPM).
3. Per-kick offset: the strongest kick-band onset within +-90 ms of each grid
   beat; median = where the kicks sit, MAD = how steadily.
4. Off-beat: ONLY from Beat This! logits when the caller has them. Signal
   features cannot tell a kick from an off-beat hat plus bass (round A2).

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 judge_grid_against_audio(): fit / wrong_bpm / shifted / off_beat / no_verdict.
    [if] the grid is at the audio's tempo [then] fit ⛔️ any finding
    [if] a 127.00 grid on 128 BPM audio [then] wrong_bpm, audio_bpm ~128.0 ⛔️ fit
    [if] a half-tempo grid [then] never wrong_bpm ⛔️ octave misreported
    [if] the grid is moved 50 ms [then] shifted with the offset ⛔️ fit
    [if] noise, or no kick on the beats [then] no_verdict ⛔️ any finding
    [if] a half-beat grid with no logits [then] never off_beat ⛔️ guessed
  ✔︎ ✅ 🎯 rule_version(): changes with any threshold.

-Claude
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from typing import Literal

import numpy as np

Verdict = Literal["fit", "wrong_bpm", "shifted", "off_beat", "no_verdict"]
VERDICTS: tuple[Verdict, ...] = ("fit", "wrong_bpm", "shifted", "off_beat", "no_verdict")
#: Verdicts a deck warns about. `shifted` is a library finding only (ADR).
DECK_WARNING_VERDICTS: tuple[Verdict, ...] = ("wrong_bpm", "off_beat")


# ---------------------------------------------------------------- config


@dataclass(frozen=True)
class GridAudioFitThresholds:
    """Every number the rule compares against; rounds in the spec set them."""

    #: Analysis window and hop. The spikes ran 512 samples at 11025 Hz.
    window_s: float = 512 / 11025
    hop_s: float = 55 / 11025
    kick_band_low_hz: float = 40.0
    kick_band_high_hz: float = 140.0
    #: Search for the audio tempo this far either side of the grid's, in steps.
    tempo_search_ratio: float = 0.025
    tempo_coarse_step_bpm: float = 0.01
    tempo_fine_step_bpm: float = 0.002
    fold_bins: int = 96
    #: No verdict below this fold sharpness (peak / mean): no steady pulse.
    min_fold_sharpness: float = 3.0
    #: No verdict when fewer grid beats than this have a clear kick near them.
    min_kick_coverage: float = 0.4
    #: A kick is clear when its onset is this many times the median onset.
    clear_kick_factor: float = 3.0
    kick_search_s: float = 0.09
    #: wrong_bpm: the audio tempo is at least this far from the grid's ...
    min_bpm_error: float = 0.1
    #: ... AND folds at least this much sharper than the grid tempo does.
    min_fold_gain: float = 1.5
    #: Where kicks sit relative to a good rekordbox grid with this detector
    #: (consensus median, round A1). Shift is judged against this, not zero.
    typical_kick_offset_ms: float = -15.0
    max_shift_ms: float = 25.0
    max_kick_spread_ms: float = 12.0
    #: off_beat: model activation at the grid lines over the half-beats below this.
    max_onbeat_ratio: float = 0.5


THRESHOLDS = GridAudioFitThresholds()


def rule_version(thresholds: GridAudioFitThresholds = THRESHOLDS) -> str:
    """Changes whenever a threshold does, so a stored verdict goes stale."""
    digest = hashlib.sha256(json.dumps(asdict(thresholds), sort_keys=True).encode("utf-8")).hexdigest()
    return f"grid-audio-fit-1:{digest[:12]}"


# ----------------------------------------------------------------- model


@dataclass(frozen=True)
class GridBeat:
    """One stored grid beat: its time and the tempo marker in force there."""

    t: float
    bpm: float


@dataclass(frozen=True)
class GridAudioFit:
    verdict: Verdict
    #: The grid's stored marker tempo (median over beats).
    grid_bpm: float
    #: The audio's best constant tempo near the grid's; None without a pulse.
    audio_bpm: float | None
    fold_sharpness_audio: float
    fold_sharpness_grid: float
    #: Median kick time minus grid line (negative: kick early, grid late).
    kick_offset_ms: float | None
    kick_spread_ms: float | None
    kick_coverage: float
    #: Model activation on the grid lines over the half-beats; None without logits.
    onbeat_ratio: float | None


# ------------------------------------------------------------ envelope


def kick_onset_envelope(pcm: np.ndarray, sr: int, thresholds: GridAudioFitThresholds = THRESHOLDS) -> np.ndarray:
    """Positive log-flux of 40-140 Hz energy, one value per hop, chunked."""
    n_fft = 1 << int(np.ceil(np.log2(thresholds.window_s * sr)))
    hop = max(1, round(thresholds.hop_s * sr))
    if len(pcm) < n_fft:
        return np.zeros(0)
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    band = (freqs >= thresholds.kick_band_low_hz) & (freqs <= thresholds.kick_band_high_hz)
    window = np.hanning(n_fft)
    n_frames = 1 + (len(pcm) - n_fft) // hop
    energy = np.empty(n_frames)
    chunk = 256
    for start in range(0, n_frames, chunk):
        idx = np.arange(start, min(start + chunk, n_frames))[:, None] * hop + np.arange(n_fft)
        spec = np.abs(np.fft.rfft(pcm[idx] * window, axis=1))
        energy[start : start + len(idx)] = spec[:, band].sum(axis=1)
    return np.maximum(np.diff(np.log1p(energy), prepend=0.0), 0.0)


# --------------------------------------------------------------- tempo


def _fold_sharpness(env: np.ndarray, frame_s: float, period_s: float, bins: int) -> float:
    t = np.arange(len(env)) * frame_s
    idx = ((t % period_s) / period_s * bins).astype(int) % bins
    sums = np.bincount(idx, weights=env, minlength=bins)
    counts = np.maximum(np.bincount(idx, minlength=bins), 1)
    folded = sums / counts
    mean = folded.mean()
    return float(folded.max() / mean) if mean > 0 else 0.0


def _best_tempo(env: np.ndarray, frame_s: float, bpm0: float, th: GridAudioFitThresholds) -> tuple[float, float]:
    def scan(candidates: np.ndarray) -> tuple[float, float]:
        scores = np.array([_fold_sharpness(env, frame_s, 60.0 / b, th.fold_bins) for b in candidates])
        i = int(np.argmax(scores))
        return float(candidates[i]), float(scores[i])

    coarse, _ = scan(
        np.arange(bpm0 * (1 - th.tempo_search_ratio), bpm0 * (1 + th.tempo_search_ratio), th.tempo_coarse_step_bpm)
    )
    span = 2 * th.tempo_coarse_step_bpm
    return scan(np.arange(coarse - span, coarse + span, th.tempo_fine_step_bpm))


# --------------------------------------------------------------- kicks


def _kick_offsets(
    env: np.ndarray, frame_s: float, beats: np.ndarray, th: GridAudioFitThresholds
) -> tuple[np.ndarray, int]:
    """Offsets (ms) of the clear kicks near each grid beat, and how many beats were in range."""
    positive = env[env > 0]
    if len(positive) == 0:
        return np.zeros(0), 0
    clear = th.clear_kick_factor * float(np.median(positive))
    w = int(th.kick_search_s / frame_s)
    offsets, in_range = [], 0
    for b in beats:
        c = int(np.rint(b / frame_s))
        if c - w < 1 or c + w >= len(env) - 1:
            continue
        in_range += 1
        seg = env[c - w : c + w + 1]
        i = int(np.argmax(seg))
        if seg[i] <= clear:
            continue
        frac = 0.0
        if 0 < i < len(seg) - 1:  # parabolic sub-frame peak
            y0, y1, y2 = seg[i - 1], seg[i], seg[i + 1]
            den = y0 - 2 * y1 + y2
            frac = 0.5 * (y0 - y2) / den if den != 0 else 0.0
        offsets.append((i + frac - w) * frame_s * 1000.0)
    return np.array(offsets), in_range


def _onbeat_ratio(logits: np.ndarray, fps: float, beats: np.ndarray, period_s: float) -> float | None:
    prob = 1.0 / (1.0 + np.exp(-np.asarray(logits, dtype=float).ravel()))  # stored as logits

    def mean_at(times: np.ndarray) -> float | None:
        vals = [
            prob[max(0, int(np.rint(t * fps)) - 1) : int(np.rint(t * fps)) + 2].max()
            for t in times
            if 0 <= int(np.rint(t * fps)) < len(prob) - 1
        ]
        return float(np.mean(vals)) if vals else None

    on, half = mean_at(beats), mean_at(beats + period_s / 2)
    if on is None or half is None or half <= 0:
        return None
    return on / half


# ----------------------------------------------------------------- rule


def _is_shifted(fit: GridAudioFit, th: GridAudioFitThresholds) -> bool:
    """Tight kicks that sit off the grid lines by more than the usual attack lead."""
    return (
        fit.kick_spread_ms is not None
        and fit.kick_offset_ms is not None
        and fit.kick_spread_ms <= th.max_kick_spread_ms
        and abs(fit.kick_offset_ms - th.typical_kick_offset_ms) > th.max_shift_ms
    )


def _verdict(fit: GridAudioFit, th: GridAudioFitThresholds) -> Verdict:
    """The rule over measured facts. Pure.

    Order matters: a wrong tempo or an off-beat grid is exactly what moves the
    kicks away from the grid lines, so kick coverage may only gate the shift
    judgement, never these two.
    """
    if fit.audio_bpm is None:
        return "no_verdict"
    if (
        abs(fit.audio_bpm - fit.grid_bpm) >= th.min_bpm_error
        and fit.fold_sharpness_audio >= th.min_fold_gain * fit.fold_sharpness_grid
    ):
        return "wrong_bpm"
    if fit.onbeat_ratio is not None and fit.onbeat_ratio < th.max_onbeat_ratio:
        return "off_beat"
    if fit.kick_coverage < th.min_kick_coverage:
        return "no_verdict"
    if _is_shifted(fit, th):
        return "shifted"
    return "fit"


def _measure(
    pcm: np.ndarray,
    sr: int,
    grid: Sequence[GridBeat],
    beat_logits: np.ndarray | None,
    logits_fps: float,
    th: GridAudioFitThresholds,
) -> GridAudioFit:
    """Every fact the rule reads, with the verdict still undecided (`no_verdict`)."""
    beats = np.array([b.t for b in grid], dtype=float)
    grid_bpm = float(np.median([b.bpm for b in grid]))
    frame_s = max(1, round(th.hop_s * sr)) / sr
    env = kick_onset_envelope(np.asarray(pcm, dtype=np.float32), sr, th)
    period_s = 60.0 / grid_bpm
    fold_grid = _fold_sharpness(env, frame_s, period_s, th.fold_bins) if len(env) else 0.0
    audio_bpm, fold_audio = _best_tempo(env, frame_s, grid_bpm, th) if len(env) else (None, 0.0)
    offsets, in_range = _kick_offsets(env, frame_s, beats, th)
    offset = float(np.median(offsets)) if len(offsets) else None
    return GridAudioFit(
        verdict="no_verdict",
        grid_bpm=grid_bpm,
        # No pulse sharp enough to name a tempo: no tempo, and so no verdict.
        audio_bpm=audio_bpm if fold_audio >= th.min_fold_sharpness else None,
        fold_sharpness_audio=fold_audio,
        fold_sharpness_grid=fold_grid,
        kick_offset_ms=offset,
        kick_spread_ms=float(np.median(np.abs(offsets - offset))) if offset is not None else None,
        kick_coverage=len(offsets) / in_range if in_range else 0.0,
        onbeat_ratio=_onbeat_ratio(beat_logits, logits_fps, beats, period_s) if beat_logits is not None else None,
    )


def judge_grid_against_audio(
    pcm: np.ndarray,
    sr: int,
    grid: Sequence[GridBeat],
    *,
    beat_logits: np.ndarray | None = None,
    logits_fps: float = 50.0,
    thresholds: GridAudioFitThresholds = THRESHOLDS,
) -> GridAudioFit:
    """The verdict for one grid on one track's mono PCM."""
    if len(grid) < 2:
        raise ValueError("judge_grid_against_audio needs a grid of at least two beats")
    fit = _measure(pcm, sr, grid, beat_logits, logits_fps, thresholds)
    return replace(fit, verdict=_verdict(fit, thresholds))
