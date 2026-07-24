# /// script
# requires-python = ">=3.10"
# dependencies = ["fast-bss-eval>=0.1.4", "soundfile>=0.12", "numpy<2"]
# ///
"""Local SI-SDR scorer for the vocal quality ladder (NOT baked into the Modal image).

Loads a reference vocal stem plus one or more estimate stems, mono-mixes, aligns
length, and prints ``{estimate_path: si_sdr_db}`` JSON to stdout. Higher dB = the
estimate is closer to the reference. Used two ways by modal_vocal_ladder.py:
  - user track:  reference = rung-10 (pseudo-reference), estimates = rungs 1..9
  - MUSDB clip:  reference = the TRUE vocal stem, estimates = all 10 rungs

Fail-fast: sample rates must match (no silent resample); a decode/shape error raises.

Run:
  uv run scripts/bench/si_sdr_score.py --reference ref.flac --estimates a.flac b.flac

Requirements (mini-PRD):
  ✔︎ si_sdr per estimate vs one reference, mono, min-length aligned, JSON to stdout.
    [if] reference SR != an estimate SR [then ⛔️] RuntimeError (no hidden resample)
    [if] a path does not exist [then ⛔️] RuntimeError listing it
  ✔︎ numpy backend only (fast_bss_eval + scipy) -- no torch dependency locally.

-Claude
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import fast_bss_eval.numpy as fast_bss_eval_numpy  # torch-free backend (top-level dispatch needs torch)
import numpy as np
import soundfile as sf


def _load_mono(path: Path) -> tuple[np.ndarray, int]:
    if not path.is_file():
        raise RuntimeError(f"audio file does not exist: {path}")
    data, sr = sf.read(str(path), dtype="float64", always_2d=True)  # (frames, channels)
    mono = data.mean(axis=1)  # (frames,)
    return mono, int(sr)


def _si_sdr_db(reference: np.ndarray, estimate: np.ndarray) -> float:
    n = min(len(reference), len(estimate))
    if n == 0:
        raise RuntimeError("zero-length overlap between reference and estimate")
    ref = reference[:n][None, :]  # (1 source, n samples)
    est = estimate[:n][None, :]
    score = fast_bss_eval_numpy.si_sdr(ref, est)  # numpy in -> numpy out, shape (1,)
    return round(float(np.asarray(score).reshape(-1)[0]), 3)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--estimates", required=True, nargs="+", type=Path)
    args = parser.parse_args()

    reference, ref_sr = _load_mono(args.reference)
    scores: dict[str, float] = {}
    for estimate_path in args.estimates:
        estimate, est_sr = _load_mono(estimate_path)
        if est_sr != ref_sr:
            raise RuntimeError(
                f"sample-rate mismatch: reference {args.reference} @ {ref_sr} Hz "
                f"vs estimate {estimate_path} @ {est_sr} Hz (refusing to resample)"
            )
        scores[str(estimate_path)] = _si_sdr_db(reference, estimate)

    json.dump(scores, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
