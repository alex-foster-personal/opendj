# /// script
# requires-python = ">=3.10"
# dependencies = ["soundfile>=0.12", "numpy<2", "scipy>=1.10"]
# ///
"""R1d: is leakage AMONG drums/bass/other real, given the shipped deck can solo them?

The claim under test: summing drums+bass+other before writing hides leakage that is
inaudible in the sum but exposed the moment a user solos one stem. data/state/stems holds
the four stems SEPARATELY for 30 of the maintainer's own tracks, so the claim is testable today with
no reference, no GPU and no spend.

Reference-free, so this can show leakage is PRESENT and size it. It cannot score quality.
Two independent readings, because either alone is arguable:

  1. Pairwise correlation between stems. Shared content shows up as correlation. The repo
     already has a precedent scale: q0-offline/leak.json measured vocals-vs-backing at
     0.045 to 0.081 inside detected regions and the blessed reading was "that is what a
     present vocal looks like", i.e. low. Anything at or under that band is not leakage.
  2. Band energy split. A stem that owns a band should hold most of that band's energy.
     Sub-80 Hz in the VOCALS stem is the least arguable leak available: sung fundamentals
     essentially never go below 80 Hz, so whatever is down there came from bass or kick.

Requirements (mini-PRD):
  ✔︎ ✅ all 30 bundles, bounded window, n stated, correlations reported against the
    existing q0 precedent band rather than against an invented threshold.
    [if] a bundle lacks one of the four stems [then ⛔️] RuntimeError naming it
  ✔︎ ✅ sub-80 Hz share reported per stem so the least-arguable leak is visible alone.

Run:
  uv run scripts/bench/r1-offline/r1d_stem_leakage.py

-Claude
"""
from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

REPO = Path(__file__).resolve().parents[3]
STEMS = REPO / "data" / "state" / "stems"
OUT = Path(__file__).resolve().parent / "r1d.json"

STEM_NAMES = ("vocals", "drums", "bass", "other")
WINDOW_S: float = 60.0
# Sung fundamentals essentially never sit below this, so vocal energy here is not vocal.
SUB_HZ: float = 80.0
# Precedent scale from scripts/bench/q0-offline/leak.json, blessed by the previous team.
Q0_PRECEDENT = (0.045, 0.081)


def _read(path: Path, window_s: float) -> tuple[np.ndarray, int]:
    if not path.is_file():
        raise RuntimeError(f"missing stem: {path}")
    info = sf.info(str(path))
    frames = min(info.frames, int(window_s * info.samplerate))
    data, sr = sf.read(str(path), frames=frames, dtype="float64", always_2d=True)
    return data.mean(axis=1), int(sr)


def abs_corr(a: np.ndarray, b: np.ndarray) -> float:
    n = min(len(a), len(b))
    x, y = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    denom = np.sqrt(np.dot(x, x) * np.dot(y, y))
    return float(abs(np.dot(x, y) / denom)) if denom > 0 else 0.0


def sub_band_share(x: np.ndarray, sr: int, cut_hz: float) -> float:
    sos = signal.butter(8, cut_hz / (sr / 2.0), btype="low", output="sos")
    low = signal.sosfiltfilt(sos, x)
    total = float(np.dot(x, x))
    return float(np.dot(low, low) / total) if total > 0 else 0.0


def main() -> None:
    bundles = sorted(p for p in STEMS.iterdir() if p.is_dir())
    corr_rows: dict[str, list[float]] = {f"{a}|{b}": [] for a, b in combinations(STEM_NAMES, 2)}
    sub_rows: dict[str, list[float]] = {s: [] for s in STEM_NAMES}
    low_band_owner: list[dict] = []

    for bundle in bundles:
        stems, sr = {}, None
        for name in STEM_NAMES:
            stems[name], sr = _read(bundle / f"{name}.wav", WINDOW_S)
        for a, b in combinations(STEM_NAMES, 2):
            corr_rows[f"{a}|{b}"].append(abs_corr(stems[a], stems[b]))
        for name in STEM_NAMES:
            sub_rows[name].append(sub_band_share(stems[name], sr, SUB_HZ))

        # How the sub-80 Hz energy is divided between the four stems. Bass and kick drum
        # legitimately own it; vocals and other holding a large share is the suspicious case.
        sos = signal.butter(8, SUB_HZ / (sr / 2.0), btype="low", output="sos")
        low_energy = {n: float(np.dot(signal.sosfiltfilt(sos, stems[n]), signal.sosfiltfilt(sos, stems[n]))) for n in STEM_NAMES}
        total_low = sum(low_energy.values())
        low_band_owner.append(
            {"bundle": bundle.name, **{n: round(low_energy[n] / total_low, 4) for n in STEM_NAMES}}
        )

    def stats(values: list[float]) -> dict:
        arr = np.asarray(values)
        return {
            "median": round(float(np.median(arr)), 4),
            "min": round(float(arr.min()), 4),
            "max": round(float(arr.max()), 4),
        }

    result = {
        "n_bundles": len(bundles),
        "window_s": WINDOW_S,
        "q0_precedent_band": Q0_PRECEDENT,
        "pairwise_abs_correlation": {k: stats(v) for k, v in corr_rows.items()},
        "sub80hz_share_of_own_energy": {k: stats(v) for k, v in sub_rows.items()},
        "sub80hz_band_ownership": {
            n: stats([r[n] for r in low_band_owner]) for n in STEM_NAMES
        },
        "per_bundle_sub80_ownership": low_band_owner,
    }

    print(f"n={result['n_bundles']} bundles, {WINDOW_S:.0f}s window")
    print(f"q0 precedent for 'not leakage': abs corr {Q0_PRECEDENT[0]} to {Q0_PRECEDENT[1]}")
    print("\npairwise |correlation| between stems (median [min, max]):")
    for k, v in result["pairwise_abs_correlation"].items():
        flag = "  <-- above q0 precedent" if v["median"] > Q0_PRECEDENT[1] else ""
        print(f"  {k:>16}  {v['median']:.4f}  [{v['min']:.4f}, {v['max']:.4f}]{flag}")
    print(f"\nshare of each stem's OWN energy below {SUB_HZ:.0f} Hz (median [min, max]):")
    for k, v in result["sub80hz_share_of_own_energy"].items():
        print(f"  {k:>16}  {v['median']:.4f}  [{v['min']:.4f}, {v['max']:.4f}]")
    print(f"\nwho owns the sub-{SUB_HZ:.0f} Hz band, as a share of it (median [min, max]):")
    for k, v in result["sub80hz_band_ownership"].items():
        print(f"  {k:>16}  {v['median']:.4f}  [{v['min']:.4f}, {v['max']:.4f}]")

    OUT.write_text(json.dumps(result, indent=2) + "\n")
    print(f"\n[done] wrote {OUT}")


if __name__ == "__main__":
    main()
