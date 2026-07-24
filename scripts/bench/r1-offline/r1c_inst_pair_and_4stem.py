# /// script
# requires-python = ">=3.10"
# dependencies = ["soundfile>=0.12", "numpy<2", "scipy>=1.10"]
# ///
"""R1c: two questions the Zeno round raised and could not answer from the ratings alone.

Q1. hdemucs_mmi at overlap 0 was scored instrumental 10; the same model at overlap 0.25 was
    scored 7. Identical vocal scores of 8. Is there a 3-point signal in those instrumentals,
    or are they the same audio rated twice? Measured as SI-SDR of one instrumental against
    the other, alongside the same figure for pairs the rater DID hear apart, which sets the
    scale for what an audible difference looks like on this material.

Q2. The rater asked whether the pipeline only does a vocal stem. Production writes four
    stems. This sizes what fraction of the four-stem product's energy the vocals-only
    evaluation has never scored, over the 30 existing bundles. Reference-free, so it sizes
    the blind spot rather than measuring quality inside it.

Reads only local files. No GPU, no network, no iCloud-evicted material (the evicted pool is
source mp3s under ~/Documents; data/state/stems is repo-local and already materialised).

Requirements (mini-PRD):
  ✔︎ ✅ Q1 compares every arm pair, so the ov0/ov25 number is read against a scale rather
    than in isolation.
    [if] a pair's file is missing [then ⛔️] RuntimeError naming it
  ✔︎ ✅ Q2 reads a bounded window per bundle so the pass stays cheap, and states n.
    [if] a bundle lacks one of the four stems [then ⛔️] RuntimeError naming the bundle

Run:
  uv run scripts/bench/r1-offline/r1c_inst_pair_and_4stem.py

-Claude
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from itertools import combinations
from pathlib import Path

import numpy as np
import soundfile as sf

REPO = Path(__file__).resolve().parents[3]
BENCH = REPO / "scripts" / "bench"
STEMS = REPO / "data" / "state" / "stems"
OUT = Path(__file__).resolve().parent / "r1c.json"

ARMS = [
    "hdemucs_mmi-ov0",
    "hdemucs_mmi-ov25",
    "htdemucs-ov25",
    "rb7-stems",
    "rb6-spleeter",
    "htdemucs-ov0-22k",
]
HUMAN_INST = {
    "hdemucs_mmi-ov0": 10,
    "hdemucs_mmi-ov25": 7,
    "htdemucs-ov25": 8,
    "rb7-stems": 8,
    "rb6-spleeter": None,
    "htdemucs-ov0-22k": 7,
}
# Bounded read per bundle so 30 four-stem bundles stay a seconds-scale pass.
WINDOW_S: float = 60.0


def _decode_mono(path: Path) -> tuple[np.ndarray, int]:
    if not path.is_file():
        raise RuntimeError(f"missing: {path}")
    if path.suffix.lower() in {".wav", ".flac"}:
        data, sr = sf.read(str(path), dtype="float64", always_2d=True)
        return data.mean(axis=1), int(sr)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav = Path(tmp.name)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-c:a", "pcm_f32le", str(wav)], check=True
    )
    try:
        data, sr = sf.read(str(wav), dtype="float64", always_2d=True)
        return data.mean(axis=1), int(sr)
    finally:
        wav.unlink(missing_ok=True)


def _align(*sigs: np.ndarray) -> list[np.ndarray]:
    n = min(len(s) for s in sigs)
    return [s[:n] for s in sigs]


def si_sdr_db(reference: np.ndarray, estimate: np.ndarray) -> float:
    ref, est = _align(reference, estimate)
    ref, est = ref - ref.mean(), est - est.mean()
    alpha = float(np.dot(est, ref) / np.dot(ref, ref))
    target = alpha * ref
    noise = est - target
    return float(10.0 * np.log10(np.dot(target, target) / max(np.dot(noise, noise), 1e-30)))


def q1_instrumental_pairs() -> dict:
    inst = {a: _decode_mono(BENCH / f"clips-cheap/zeno-signs-{a}.inst.m4a")[0] for a in ARMS}
    voc = {a: _decode_mono(BENCH / f"clips-cheap/zeno-signs-{a}.m4a")[0] for a in ARMS}
    rows = []
    for a, b in combinations(ARMS, 2):
        rows.append(
            {
                "pair": f"{a} vs {b}",
                "inst_si_sdr_db": round(si_sdr_db(inst[a], inst[b]), 2),
                "vocal_si_sdr_db": round(si_sdr_db(voc[a], voc[b]), 2),
                "human_inst_gap": (
                    None
                    if HUMAN_INST[a] is None or HUMAN_INST[b] is None
                    else abs(HUMAN_INST[a] - HUMAN_INST[b])
                ),
            }
        )
    rows.sort(key=lambda r: -r["inst_si_sdr_db"])
    return {"pairs": rows, "note": "higher dB = the two arms produced more nearly the same audio"}


def q2_four_stem_blind_spot() -> dict:
    bundles = sorted(p for p in STEMS.iterdir() if p.is_dir())
    rows = []
    for bundle in bundles:
        energies = {}
        for stem in ("vocals", "drums", "bass", "other"):
            path = bundle / f"{stem}.wav"
            if not path.is_file():
                raise RuntimeError(f"bundle {bundle.name} lacks {stem}.wav")
            info = sf.info(str(path))
            frames = min(info.frames, int(WINDOW_S * info.samplerate))
            data, _ = sf.read(str(path), frames=frames, dtype="float64", always_2d=True)
            energies[stem] = float(np.sum(data.mean(axis=1) ** 2))
        total = sum(energies.values())
        rows.append(
            {
                "bundle": bundle.name,
                "vocal_energy_frac": round(energies["vocals"] / total, 4),
                "unscored_energy_frac": round(1.0 - energies["vocals"] / total, 4),
            }
        )
    fracs = [r["unscored_energy_frac"] for r in rows]
    return {
        "n_bundles": len(rows),
        "window_s": WINDOW_S,
        "median_unscored_energy_frac": round(float(np.median(fracs)), 4),
        "min": round(min(fracs), 4),
        "max": round(max(fracs), 4),
        "rows": rows,
    }


def main() -> None:
    print("[Q1] how different are the instrumentals the rater split 10 vs 7 ...")
    q1 = q1_instrumental_pairs()
    for r in q1["pairs"]:
        print(f"  {r['pair']:>46}  inst {r['inst_si_sdr_db']:>7.2f} dB   vocal {r['vocal_si_sdr_db']:>7.2f} dB   human inst gap {r['human_inst_gap']}")

    print("[Q2] fraction of the four-stem product never scored by a vocals-only eval ...")
    q2 = q2_four_stem_blind_spot()
    print(f"  n={q2['n_bundles']} bundles, median unscored energy fraction {q2['median_unscored_energy_frac']} (min {q2['min']}, max {q2['max']})")

    OUT.write_text(json.dumps({"q1_instrumental_pairs": q1, "q2_four_stem_blind_spot": q2}, indent=2) + "\n")
    print(f"[done] wrote {OUT}")


if __name__ == "__main__":
    main()
