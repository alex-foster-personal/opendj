# /// script
# requires-python = ">=3.10"
# dependencies = ["soundfile>=0.12", "numpy<2"]
# ///
"""R1e: is the per-stem mixture floor a MEASUREMENT, or an identity in stem loudness?

four_stem_ladder.json reports a mixture floor per stem (vocals -8.38, drums -5.48,
bass -10.63, other +1.44 on Timboz - Pony) and the proposal is to normalise cross-stem
comparisons by gain over that floor. Whether that normaliser is sound depends entirely on
what the floor is.

If the mixture is M = S + N with target S and everything-else N, and S is roughly
orthogonal to N, then SI-SDR of M scored against S collapses to 10*log10(||S||^2/||N||^2),
which is just the stem's energy ratio against the rest of the mix. If that holds, the
floor carries NO information about how hard a stem is to separate; it only says how loud
the stem is. Gain over floor would then be exactly loudness-corrected SI-SDR, no more.

Tested on all 30 four-stem bundles, 120 stem-level points, by reconstructing the mixture
as the sum of the four stems (the same reconstruction q0-offline uses).

Requirements (mini-PRD):
  ✔︎ ✅ identity checked numerically per stem, deviation reported, not asserted.
    [if] max abs deviation > 0.5 dB [then] the identity does NOT hold and the floor
      carries information beyond loudness; report that instead of the identity
  ✔︎ ✅ per-stem loudness distribution reported, so a systematic bias across stems
    (quiet stems flattered, dominant stems punished) is visible rather than argued.

Run:
  uv run scripts/bench/r1-offline/r1e_floor_identity.py

-Claude
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import soundfile as sf

REPO = Path(__file__).resolve().parents[3]
STEMS_DIR = REPO / "data" / "state" / "stems"
OUT = Path(__file__).resolve().parent / "r1e.json"

STEM_NAMES = ("vocals", "drums", "bass", "other")
WINDOW_S: float = 60.0
IDENTITY_TOL_DB: float = 0.5


def _read(path: Path) -> np.ndarray:
    if not path.is_file():
        raise RuntimeError(f"missing stem: {path}")
    info = sf.info(str(path))
    frames = min(info.frames, int(WINDOW_S * info.samplerate))
    data, _ = sf.read(str(path), frames=frames, dtype="float64", always_2d=True)
    return data.mean(axis=1)


def si_sdr_db(reference: np.ndarray, estimate: np.ndarray) -> float:
    n = min(len(reference), len(estimate))
    ref, est = reference[:n], estimate[:n]
    ref, est = ref - ref.mean(), est - est.mean()
    alpha = float(np.dot(est, ref) / np.dot(ref, ref))
    target = alpha * ref
    noise = est - target
    return float(10.0 * np.log10(np.dot(target, target) / max(np.dot(noise, noise), 1e-30)))


def main() -> None:
    bundles = sorted(p for p in STEMS_DIR.iterdir() if p.is_dir())
    per_stem: dict[str, list[dict]] = {s: [] for s in STEM_NAMES}
    deviations: list[float] = []

    for bundle in bundles:
        stems = {n: _read(bundle / f"{n}.wav") for n in STEM_NAMES}
        length = min(len(v) for v in stems.values())
        stems = {n: v[:length] for n, v in stems.items()}
        mixture = sum(stems.values())

        for name in STEM_NAMES:
            target = stems[name]
            rest = mixture - target
            measured_floor = si_sdr_db(target, mixture)
            loudness_ratio_db = 10.0 * np.log10(np.dot(target, target) / max(np.dot(rest, rest), 1e-30))
            deviation = abs(measured_floor - loudness_ratio_db)
            deviations.append(deviation)
            per_stem[name].append(
                {
                    "bundle": bundle.name,
                    "mixture_floor_db": round(measured_floor, 3),
                    "loudness_ratio_db": round(float(loudness_ratio_db), 3),
                    "deviation_db": round(deviation, 4),
                }
            )

    max_dev = max(deviations)
    identity_holds = max_dev <= IDENTITY_TOL_DB

    def stats(values: list[float]) -> dict:
        arr = np.asarray(values)
        return {
            "median": round(float(np.median(arr)), 2),
            "min": round(float(arr.min()), 2),
            "max": round(float(arr.max()), 2),
        }

    result = {
        "n_bundles": len(bundles),
        "n_points": len(deviations),
        "identity_holds": bool(identity_holds),
        "max_abs_deviation_db": round(max_dev, 4),
        "median_abs_deviation_db": round(float(np.median(deviations)), 4),
        "tolerance_db": IDENTITY_TOL_DB,
        "mixture_floor_by_stem_db": {n: stats([r["mixture_floor_db"] for r in per_stem[n]]) for n in STEM_NAMES},
        "per_stem": per_stem,
    }

    print(f"n={result['n_bundles']} bundles, {result['n_points']} stem-level points")
    print(f"\nfloor vs pure loudness ratio: max deviation {max_dev:.4f} dB, median {result['median_abs_deviation_db']:.4f} dB")
    print(f"identity holds within {IDENTITY_TOL_DB} dB: {identity_holds}")
    print("\nmixture floor by stem, dB (median [min, max]):")
    for n, v in result["mixture_floor_by_stem_db"].items():
        print(f"  {n:>8}  {v['median']:>7.2f}  [{v['min']:>7.2f}, {v['max']:>7.2f}]")

    OUT.write_text(json.dumps(result, indent=2) + "\n")
    print(f"\n[done] wrote {OUT}")


if __name__ == "__main__":
    main()
