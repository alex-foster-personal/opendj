# /// script
# requires-python = ">=3.10"
# dependencies = ["soundfile>=0.12", "numpy<2"]
# ///
"""Pick the most vocal-dense fixed-length window of a TRUE vocal stem.

Used by the MUSDB absolute ladder: the excerpt window is chosen from the ground
truth vocals (not from a separator's guess), then the SAME window is cut from
the mixture and from the truth. Ranking is by total vocal energy inside the
window, which is the honest definition of "vocal dense" when a true stem exists.

Prints ``{"start_s", "end_s", "length_s", "rms_dbfs", "silent_frac"}`` JSON, plus
``{"prominence_db", "prominence_gate"}`` when ``--mixture`` is supplied.

PROMINENCE GATE (added after the bass round wasted a listening slot). the maintainer's
verdict on the Timboz bass round was that the bassline was quiet and buried and
"would rarely be used", so every arm failing told us nothing about whether the
product is usable. A stem that is merely PRESENT is not a valid test subject: it
must be PROMINENT and REPRESENTATIVE of how the stem would actually be used.
Prominence is measured as the stem's energy inside the chosen window relative to
the mixture's energy in the SAME window, in dB. A buried stem sits far below the
mix; a bassline a DJ would solo sits near it. The gate runs ABOVE the not-silent
check, because "audible at all" is a much weaker bar than "worth testing".
The default bar is calibrated on measured data, see MIN_PROMINENCE_DB.

Requirements (mini-PRD):
  ✔︎ ✅ slides a window of --length seconds at --step over the hop-wise energy of
    the mono-mixed stem and returns the maximum-energy window.
    [if] the file is shorter than --length [then ⛔️] RuntimeError (no silent clamp)
    [if] the chosen window is pure digital silence [then ⛔️] RuntimeError
  ✔︎ ✅ reports rms_dbfs of the winner plus the fraction of its hops below -60 dBFS
    so a caller can see how sung-through the window actually is.
  ✔︎ ✅ MATERIAL-SELECTION gate: with --mixture, refuses a window where the stem is
    buried in the mix, before the not-silent check runs.
    [if] the stem sits more than --min-prominence-db below the mixture inside the
      window [then ⛔️] RuntimeError naming the measured dB (no warn-and-continue)
    [if] --mixture is a different sample rate or length than --audio [then ⛔️]
      RuntimeError (no silent resample, no silent truncation)
    [if] --mixture is omitted [then] prominence is not reported and not gated, and
      the caller is choosing to run an ungated selection

Run:
  uv run scripts/bench/pick_vocal_window.py --audio vocals.wav --length 75
  uv run scripts/bench/pick_vocal_window.py --audio bass.wav --mixture mixture.wav

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

# How far below the full mixture a stem may sit inside its own best window and
# still be worth a listening slot. MEASURED calibration, n=3 rounds, whole rated
# window, mono, against each round's own mixture:
#
#   zeno-signs   vocals   -5.49 dB   round WORKED (the maintainer gave discriminating labels)
#   am-contra    vocals   -5.50 dB   round WORKED (the maintainer gave discriminating labels)
#   timboz-pony  bass    -11.30 dB   round FAILED ("would rarely be used")
#
# -8.0 dB is the midpoint of that gap: it clears both productive rounds by ~2.5 dB
# and rejects the unproductive one by 3.3 dB. n=3 is thin and the bar is a
# hypothesis, not a law -- it is recorded as judgement type J09 in
# judgement-calibration.json with confidence marked accordingly.
MIN_PROMINENCE_DB: float = -8.0


def _hop_energy(path: Path, hop_s: float) -> tuple[np.ndarray, int]:
    """Mono mean-square per hop_s frame. Returns (energy per hop, sample rate)."""
    if not path.is_file():
        raise RuntimeError(f"audio file does not exist: {path}")
    data, sr = sf.read(str(path), dtype="float64", always_2d=True)
    mono = data.mean(axis=1)
    hop = round(sr * hop_s)
    n_hops = len(mono) // hop
    if n_hops == 0:
        raise RuntimeError(f"{path} is shorter than one {hop_s}s hop")
    trimmed = mono[: n_hops * hop].reshape(n_hops, hop)
    return (trimmed**2).mean(axis=1), int(sr)


def _dbfs(mean_square: float) -> float:
    return -math.inf if mean_square <= 0 else round(10.0 * math.log10(mean_square), 2)


def prominence_db(stem_window: np.ndarray, mixture_window: np.ndarray) -> float:
    """Stem energy relative to mixture energy over the same hops, in dB.

    0 dB means the stem IS the mix in that window (a solo passage). Large
    negatives mean the stem is buried under everything else playing.
    """
    mix_ms = float(mixture_window.mean())
    if mix_ms <= 0:
        raise RuntimeError(
            "the mixture is digital silence inside the chosen window, so stem "
            "prominence is undefined -- the two files are not the same take"
        )
    return round(_dbfs(float(stem_window.mean())) - _dbfs(mix_ms), 2)


def pick_window(
    energy: np.ndarray,
    hop_s: float,
    length_s: float,
    step_s: float,
    mixture_energy: np.ndarray | None = None,
    min_prominence_db: float = MIN_PROMINENCE_DB,
) -> dict[str, float]:
    """Max-energy window of length_s, slid at step_s over the hop energy series."""
    hops_per_window = round(length_s / hop_s)
    hops_per_step = max(1, round(step_s / hop_s))
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

    out: dict[str, float] = {}
    # MATERIAL-SELECTION gate, deliberately ABOVE the not-silent check below: a
    # buried stem passes "not silent" and still burns a listening slot, which is
    # the exact failure the Timboz bass round hit.
    if mixture_energy is not None:
        if len(mixture_energy) != len(energy):
            raise RuntimeError(
                f"mixture has {len(mixture_energy)} hops and the stem has {len(energy)}: "
                "they are not the same take, refusing to compare (no silent truncation)"
            )
        measured = prominence_db(window, mixture_energy[best : best + hops_per_window])
        if measured < min_prominence_db:
            raise RuntimeError(
                f"MATERIAL REJECTED: the stem sits {measured:.2f} dB below the mixture "
                f"in its own loudest {length_s:.1f}s window, under the "
                f"{min_prominence_db:.1f} dB prominence bar. This stem is present but "
                "BURIED, so a listening round on it grades a part the maintainer would rarely "
                "use and cannot inform a product decision. Pick material where this "
                "stem is prominent and representative, or lower --min-prominence-db "
                "deliberately and record why."
            )
        out["prominence_db"] = measured
        out["prominence_gate"] = round(min_prominence_db, 2)

    if mean_square <= 0:
        raise RuntimeError(
            f"the loudest {length_s:.1f}s window is digital silence -- the stem "
            "carries no vocal energy at all"
        )
    silent_hops = int((window < 10 ** (SILENCE_DBFS / 10.0)).sum())
    out.update({
        "start_s": round(best * hop_s, 2),
        "end_s": round((best + hops_per_window) * hop_s, 2),
        "length_s": round(hops_per_window * hop_s, 2),
        "rms_dbfs": _dbfs(mean_square),
        "silent_frac": round(silent_hops / len(window), 3),
        "source_duration_s": round(total_s, 2),
    })
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--length", type=float, default=75.0)
    parser.add_argument("--step", type=float, default=1.0)
    parser.add_argument(
        "--mixture",
        type=Path,
        default=None,
        help="the full mixture this stem came from. Supplying it turns on the "
        "material-selection prominence gate.",
    )
    parser.add_argument(
        "--min-prominence-db",
        type=float,
        default=MIN_PROMINENCE_DB,
        help="how far below the mixture the stem may sit and still be worth a "
        f"listening slot (default {MIN_PROMINENCE_DB})",
    )
    args = parser.parse_args()

    energy, sr = _hop_energy(args.audio, HOP_S)
    if args.mixture is None:
        mixture_energy = None
    else:
        mixture_energy, mix_sr = _hop_energy(args.mixture, HOP_S)
        if mix_sr != sr:
            raise RuntimeError(
                f"{args.mixture} is {mix_sr} Hz and {args.audio} is {sr} Hz: "
                "refusing to resample silently"
            )
    json.dump(
        pick_window(
            energy,
            HOP_S,
            args.length,
            args.step,
            mixture_energy=mixture_energy,
            min_prominence_db=args.min_prominence_db,
        ),
        sys.stdout,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
