"""Own-grid phase refinement: slide the fitted grid onto the kick (GRID-PHASE-01).

The own grid's BPM is right far more often than its phase. Beat This! runs at
50 frames a second and our served line carries a fixed -10 ms offset, so a
track's grid lands anywhere from 13 ms late to 23 ms early of its kick
(measured Wed 4 Nov 2026 on "Viva - The cut"). Two synced tracks then flam by
the sum. This module measures where the kick's click sits relative to every
grid line and returns one constant shift.

METHOD. The kick's attack transient lives in 1-4 kHz (the sub-bass body rises
over 50-100 ms and cannot place a line to a few ms). Band energy over a 5.8 ms
window every 1 ms gives a click envelope; folding it around every grid beat
within +-`MAX_SHIFT_MS` gives the average kick shape; its peak is the shift.
Accepted only when that peak is clear (`MIN_SHARPNESS` x the fold's median),
so a track with no steady kick keeps its grid. `PEAK_TO_LINE_MS` places the
line where Mixed In Key places it relative to the same envelope peak, the
ground truth this was calibrated on ((private path)).

Pure numpy, no scipy: the installed app ships numpy only.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 refine_phase(): one constant shift that puts the grid on the kick.
    [if] synthetic kicks sit 12 ms after the grid lines [then] shift +12 ms (+-1) ⛔️ unmoved
    [if] the kicks sit 40 ms away (beyond the limit) [then] not accepted ⛔️ a 30 ms clamp served
    [if] there is no kick (noise) [then] not accepted ⛔️ a shift chasing noise
  ✔︎ ✅ 🎯 matches Mixed In Key within 3 ms on the measured Viva tracks with a clear kick.

-Claude
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np

# ----------------------------------------------------------------- config

#: Decode rate the refinement expects (mono float).
SAMPLE_RATE: Final = 22050
#: Click band, Hz.
BAND_HZ: Final = (1000.0, 4000.0)
#: Envelope frame and hop.
WINDOW: Final = 128
HOP: Final = 22
#: The grid moves at most this far; a bigger error is a wrong grid, not a phase.
MAX_SHIFT_MS: Final = 30.0
#: Fold peak over fold median below which there is no clear kick.
#: Set on the Viva measurement: every fold under 2.1 was ambiguous against MIK,
#: every one over 3.1 put the grid within 3 ms of a clean MIK grid.
MIN_SHARPNESS: Final = 3.0
#: Shifts smaller than this are not worth serving.
MIN_SHIFT_MS: Final = 1.0
#: Where the line sits relative to the click envelope's peak (calibrated on MIK).
PEAK_TO_LINE_MS: Final = 0.0
#: Fewest beats in range to fold.
MIN_BEATS: Final = 32


@dataclass(frozen=True)
class PhaseRefinement:
    shift_ms: float
    sharpness: float
    accepted: bool
    reason: str


# ----------------------------------------------------------------- envelope


def click_envelope(pcm: np.ndarray, sr: int = SAMPLE_RATE) -> tuple[np.ndarray, float]:
    """(1-4 kHz band energy per hop, frames per second). Frame i is centred at i*HOP + WINDOW/2."""
    if pcm.ndim != 1:
        raise ValueError(f"click_envelope wants mono PCM, got shape {pcm.shape}")
    if len(pcm) < WINDOW:
        return np.zeros(0), sr / HOP
    freqs = np.fft.rfftfreq(WINDOW, 1 / sr)
    band = (freqs >= BAND_HZ[0]) & (freqs <= BAND_HZ[1])
    window = np.hanning(WINDOW).astype(np.float32)
    n_frames = 1 + (len(pcm) - WINDOW) // HOP
    out = np.empty(n_frames, dtype=np.float32)
    offsets = np.arange(WINDOW)
    chunk = 4096
    for start in range(0, n_frames, chunk):
        idx = np.arange(start, min(start + chunk, n_frames))[:, None] * HOP + offsets
        spec = np.fft.rfft(pcm[idx] * window, axis=1)[:, band]
        out[start : start + len(idx)] = (spec.real**2 + spec.imag**2).sum(axis=1)
    return out, sr / HOP


def _lowest_priority() -> None:
    os.nice(19)


def decode_mono(path: Path, ffmpeg: str) -> np.ndarray:
    """``path`` as mono float32 at `SAMPLE_RATE`, decoded by ffmpeg at the lowest CPU priority.

    ffmpeg trims an MP3's encoder lead-in exactly as the engine's decoder does,
    so these samples share the timeline the grid is played against. Lowest
    priority because this runs while a set is playing: a starved decode is
    slower, a starved audio thread is a glitch.
    """
    argv = [ffmpeg, "-v", "error", "-nostdin", "-threads", "1", "-i", str(path), "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"]
    if sys.platform == "win32":
        done = subprocess.run(argv, capture_output=True, check=False, creationflags=subprocess.IDLE_PRIORITY_CLASS)
    else:
        done = subprocess.run(argv, capture_output=True, check=False, preexec_fn=_lowest_priority)
    if done.returncode != 0:
        raise RuntimeError(f"ffmpeg could not decode {path}: {done.stderr.decode(errors='replace').strip()[-300:]}")
    return np.frombuffer(done.stdout, dtype=np.float32)


# ----------------------------------------------------------------- refine


def refine_phase(env: np.ndarray, rate: float, beats_s: Sequence[float]) -> PhaseRefinement:
    """The constant shift (ms, + = later) that puts ``beats_s`` on the kick."""
    centre_s = (WINDOW / 2) / SAMPLE_RATE
    reach = round(MAX_SHIFT_MS * 2 / 1000 * rate)
    b = np.asarray(beats_s, dtype=float)
    frames = np.round((b - centre_s) * rate).astype(int)
    frames = frames[(frames - reach >= 0) & (frames + reach < len(env))]
    if len(frames) < MIN_BEATS:
        return PhaseRefinement(0.0, 0.0, False, f"only {len(frames)} beats inside the audio")
    lags = np.arange(-reach, reach + 1)
    fold = env[frames[:, None] + lags[None, :]].mean(axis=0)
    median = float(np.median(fold)) or 1e-12
    peak = int(np.argmax(fold))
    sharpness = float(fold[peak] / median)
    # Sub-frame peak by parabola through the three samples round it.
    frac = 0.0
    if 0 < peak < len(fold) - 1:
        a, c, d = fold[peak - 1], fold[peak], fold[peak + 1]
        denom = a - 2 * c + d
        frac = 0.5 * (a - d) / denom if denom != 0 else 0.0
    shift_ms = round(float((lags[peak] + frac) / rate * 1000 + PEAK_TO_LINE_MS), 1)
    if sharpness < MIN_SHARPNESS:
        return PhaseRefinement(shift_ms, round(sharpness, 2), False, f"no clear kick (sharpness {sharpness:.2f} < {MIN_SHARPNESS})")
    if abs(shift_ms) > MAX_SHIFT_MS:
        return PhaseRefinement(shift_ms, round(sharpness, 2), False, f"kick sits {shift_ms:+.1f} ms off, beyond {MAX_SHIFT_MS:.0f} ms")
    if abs(shift_ms) < MIN_SHIFT_MS:
        return PhaseRefinement(shift_ms, round(sharpness, 2), False, f"grid already on the kick ({shift_ms:+.1f} ms)")
    return PhaseRefinement(shift_ms, round(sharpness, 2), True, f"grid moved {shift_ms:+.1f} ms onto the kick")


__all__ = ["MAX_SHIFT_MS", "MIN_SHARPNESS", "SAMPLE_RATE", "PhaseRefinement", "click_envelope", "decode_mono", "refine_phase"]
