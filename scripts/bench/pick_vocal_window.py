# /// script
# requires-python = ">=3.10"
# dependencies = ["soundfile>=0.12", "numpy<2"]
# ///
"""Pick the most vocal-dense fixed-length window of a TRUE vocal stem.

Used by the MUSDB absolute ladder: the excerpt window is chosen from the ground
truth vocals (not from a separator's guess), then the SAME window is cut from
the mixture and from the truth. Ranking is by total vocal energy inside the
window, which is the honest definition of "vocal dense" when a true stem exists.

Prints ``{"start_s", "end_s", "length_s", "rms_dbfs", "silent_frac"}`` JSON.

Requirements (mini-PRD):
  ✔︎ ✅ slides a window of --length seconds at --step over the hop-wise energy of
    the mono-mixed stem and returns the maximum-energy window.
    [if] the file is shorter than --length [then ⛔️] RuntimeError (no silent clamp)
    [if] the chosen window is pure digital silence [then ⛔️] RuntimeError
  ✔︎ ✅ reports rms_dbfs of the winner plus the fraction of its hops below -60 dBFS
    so a caller can see how sung-through the window actually is.

Run:
  uv run scripts/bench/pick_vocal_window.py --audio vocals.wav --length 75

-Claude
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

HOP_S: float = 0.5
SILENCE_DBFS: float = -60.0


def _hop_energy(path: Path, hop_s: float) -> tuple[np.ndarray, int]:
    """Mono mean-square per hop_s frame. Returns (energy per hop, sample rate)."""
    if not path.is_file():
        raise RuntimeError(f"audio file does not exist: {path}")
    data, sr = sf.read(str(path), dtype="float64", always_2d=True)
    mono = data.mean(axis=1)
    hop = int(round(sr * hop_s))
    n_hops = len(mono) // hop
    if n_hops == 0:
        raise RuntimeError(f"{path} is shorter than one {hop_s}s hop")
    trimmed = mono[: n_hops * hop].reshape(n_hops, hop)
    return (trimmed**2).mean(axis=1), int(sr)


def _dbfs(mean_square: float) -> float:
    return -math.inf if mean_square <= 0 else round(10.0 * math.log10(mean_square), 2)


def pick_window(
    energy: np.ndarray, hop_s: float, length_s: float, step_s: float
) -> dict[str, float]:
    """Max-energy window of length_s, slid at step_s over the hop energy series."""
    hops_per_window = int(round(length_s / hop_s))
    hops_per_step = max(1, int(round(step_s / hop_s)))
    total_s = len(energy) * hop_s
    if len(energy) < hops_per_window:
        raise RuntimeError(
            f"stem is {total_s:.1f}s, shorter than the requested {length_s:.1f}s window"
        )
    cumulative = np.concatenate(([0.0], np.cumsum(energy)))
    starts = np.arange(0, len(energy) - hops_per_window + 1, hops_per_step)
    sums = cumulative[starts + hops_per_window] - cumulative[starts]
    best = int(starts[int(np.argmax(sums))])
    window = energy[best : best + hops_per_window]
    mean_square = float(window.mean())
    if mean_square <= 0:
        raise RuntimeError(
            f"the loudest {length_s:.1f}s window is digital silence -- the stem "
            "carries no vocal energy at all"
        )
    silent_hops = int((window < 10 ** (SILENCE_DBFS / 10.0)).sum())
    return {
        "start_s": round(best * hop_s, 2),
        "end_s": round((best + hops_per_window) * hop_s, 2),
        "length_s": round(hops_per_window * hop_s, 2),
        "rms_dbfs": _dbfs(mean_square),
        "silent_frac": round(silent_hops / len(window), 3),
        "source_duration_s": round(total_s, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--length", type=float, default=75.0)
    parser.add_argument("--step", type=float, default=1.0)
    args = parser.parse_args()

    energy, _sr = _hop_energy(args.audio, HOP_S)
    json.dump(pick_window(energy, HOP_S, args.length, args.step), sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
