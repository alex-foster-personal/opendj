# /// script
# requires-python = ">=3.11"
# dependencies = ["torch", "demucs", "numpy<2", "soundfile", "scipy", "fast-bss-eval>=0.1.4"]
# ///
"""R1f: what is this pipeline's run-to-run noise floor, and does SI-SDR transfer across hardware?

Nothing in this repo has ever been measured twice. model_shootout.json has no repeat, no
run_idx, no trial and no seed field, so every paired result on the board rests on an
UNTESTED assumption that separation is deterministic. Deltas currently being quoted run as
small as +0.008 dB. If run-to-run noise is anywhere near that, those results are rounding.

Two questions, one run:
  Q1 REPEATABILITY. Separate the identical window N times in SEPARATE PROCESSES with
     nothing varied, and report both the SI-SDR standard deviation and whether the output
     waveforms are bit-identical. Separate processes, not a loop in one process, because
     production separates in a fresh process and process-level effects (thread scheduling,
     memory layout, kernel autotuning) are the plausible noise source.
  Q2 HARDWARE TRANSFER. The shootout that every model ranking rests on ran on a GTX 1660.
     Production runs elsewhere. Re-scoring the same model on the same window on different
     hardware says whether a dB figure survives the move. This is the larger risk: it is
     unmeasured, and it is assumed every time a shootout number is quoted.

Uses the lossless MUSDB18-HQ source WAVs, never the AAC listening clips, because AAC
reconstruction error sits inside the band the metrics occupy.

Requirements (mini-PRD):
  ✔︎ ✅ N separate processes, identical inputs, SI-SDR and sha256 reported per run.
    [if] a run fails [then ⛔️] RuntimeError, no partial average
    [if] the source or truth WAV is missing [then ⛔️] RuntimeError naming it
  ✔︎ ✅ stdev reported next to the specific deltas it licenses or kills, computed from
    the runs rather than typed in.
    [if] fewer than 2 successful runs [then ⛔️] RuntimeError, a stdev needs n>=2

Run:
  uv run scripts/bench/r1-offline/r1f_determinism.py --runs 3
  uv run scripts/bench/r1-offline/r1f_determinism.py --worker --out /tmp/x.wav   # internal

-Claude
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
MUSDB = REPO / ".tmp" / "bench" / "musdb-hq" / "musdb-extract"
OUT = HERE / "r1f.json"

# The exact window and config the shootout used for this track, so the fresh numbers are
# directly comparable to the recorded GTX 1660 figure rather than merely similar.
TRACK = "Zeno - Signs"
WINDOW_START_S = 109.0
WINDOW_END_S = 169.0
MODEL = "hdemucs_mmi"
OVERLAP = 0.25
# MODEL-SHOOTOUT.md, hdemucs_mmi on this track and window, GTX 1660, overlap 0.25, shifts 0.
SHOOTOUT_GTX1660_SI_SDR = 9.78
# Hash the SAMPLE DATA, never the file. libsndfile writes a PEAK chunk carrying a wall-clock
# timestamp, so a whole-file hash differs between identical runs and reads as nondeterminism.
# The first version of this script did exactly that and would have reported the opposite of
# the truth; the tell was SI-SDR agreeing to 6 decimal places while the file hashes disagreed.
QUOTED_DELTAS = {
    "hdemucs_mmi vs htdemucs_ft, n=6 paired median": 0.07,
    "hdemucs_mmi vs htdemucs, n=6 paired mean": 0.328,
    "hdemucs_mmi vs htdemucs_ft, n=6 paired mean": 0.104,
    "four-stem ov25 vs ov0, vocals, n=1": 0.008,
    "four-stem ov25 vs ov0, bass, n=1": 0.119,
    "project inaudibility threshold": 0.2,
}


def _load_window(path: Path) -> tuple[np.ndarray, int]:
    if not path.is_file():
        raise RuntimeError(f"source audio missing: {path}")
    info = sf.info(str(path))
    start = int(WINDOW_START_S * info.samplerate)
    frames = int((WINDOW_END_S - WINDOW_START_S) * info.samplerate)
    data, sr = sf.read(str(path), start=start, frames=frames, dtype="float32", always_2d=True)
    return data, int(sr)


def si_sdr_db(reference: np.ndarray, estimate: np.ndarray) -> float:
    n = min(len(reference), len(estimate))
    ref, est = reference[:n].astype(np.float64), estimate[:n].astype(np.float64)
    ref, est = ref - ref.mean(), est - est.mean()
    alpha = float(np.dot(est, ref) / np.dot(ref, ref))
    target = alpha * ref
    noise = est - target
    return float(10.0 * np.log10(np.dot(target, target) / max(np.dot(noise, noise), 1e-30)))


def worker(out_path: Path, device: str) -> None:
    """Separate the fixed window once and write the vocals stem. Run as its own process."""
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    mix, sr = _load_window(MUSDB / f"{TRACK}_mixture.wav")
    model = get_model(MODEL)
    model.eval()
    tensor = torch.from_numpy(mix.T).unsqueeze(0)
    started = time.perf_counter()
    with torch.no_grad():
        out = apply_model(
            model, tensor, overlap=OVERLAP, shifts=0, split=True,
            progress=False, device=device, num_workers=0,
        )[0]
    elapsed = time.perf_counter() - started
    vocals = out[model.sources.index("vocals")].cpu().numpy().T
    sf.write(str(out_path), vocals, sr, subtype="FLOAT")
    print(json.dumps({"separate_s": round(elapsed, 3), "sr": sr}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if args.worker:
        worker(args.out, args.device)
        return

    truth, truth_sr = _load_window(MUSDB / f"{TRACK}_vocals.wav")
    truth_mono = truth.mean(axis=1)

    scratch = HERE / ".tmp-determinism"
    scratch.mkdir(exist_ok=True)
    runs: list[dict] = []
    for i in range(args.runs):
        out_path = scratch / f"run{i}.wav"
        print(f"  run {i + 1}/{args.runs} on {args.device} ...", flush=True)
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker",
             "--out", str(out_path), "--device", args.device],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"run {i} failed:\n{proc.stdout}\n{proc.stderr}")
        meta = json.loads(proc.stdout.strip().splitlines()[-1])
        est, est_sr = sf.read(str(out_path), dtype="float32", always_2d=True)
        if est_sr != truth_sr:
            raise RuntimeError(f"sample-rate mismatch: truth {truth_sr}, run {est_sr}")
        runs.append(
            {
                "run": i,
                "si_sdr": round(si_sdr_db(truth_mono, est.mean(axis=1)), 6),
                "sample_sha256": hashlib.sha256(est.tobytes()).hexdigest()[:16],
                "separate_s": meta["separate_s"],
            }
        )
        print(f"    si_sdr {runs[-1]['si_sdr']:.6f} dB  sample sha {runs[-1]['sample_sha256']}  {meta['separate_s']:.1f}s")

    if len(runs) < 2:
        raise RuntimeError("a standard deviation needs at least 2 runs")

    scores = [r["si_sdr"] for r in runs]
    hashes = {r["sample_sha256"] for r in runs}
    stdev = statistics.stdev(scores)
    spread = max(scores) - min(scores)
    mean = statistics.mean(scores)

    # Two separate judgements, deliberately not collapsed into one. A zero noise floor makes
    # every delta a REAL measurement, which says nothing about whether it is a MEANINGFUL one.
    # Reporting only the first would license a 0.008 dB result as though it mattered.
    def judge(delta: float) -> str:
        real = "real, above noise" if abs(delta) > 3 * max(stdev, 1e-12) else "inside noise, not measurable"
        audible = "and above the 0.2 dB inaudibility bar" if abs(delta) >= 0.2 else "but below the 0.2 dB inaudibility bar"
        return f"{real}, {audible}"

    verdict = {f"{k} ({v} dB)": judge(v) for k, v in QUOTED_DELTAS.items()}
    result = {
        "track": TRACK,
        "window_s": [WINDOW_START_S, WINDOW_END_S],
        "model": MODEL,
        "overlap": OVERLAP,
        "device": args.device,
        "n_runs": len(runs),
        "runs": runs,
        "bit_identical": len(hashes) == 1,
        "si_sdr_mean": round(mean, 6),
        "si_sdr_stdev": round(stdev, 6),
        "si_sdr_spread": round(spread, 6),
        "hardware_transfer": {
            "shootout_gtx1660_si_sdr": SHOOTOUT_GTX1660_SI_SDR,
            "this_hardware_mean": round(mean, 3),
            "delta_db": round(mean - SHOOTOUT_GTX1660_SI_SDR, 3),
        },
        "quoted_delta_verdicts": verdict,
    }

    print(f"\n  bit-identical across {len(runs)} separate processes: {result['bit_identical']}")
    print(f"  SI-SDR mean {mean:.6f} dB, stdev {stdev:.6f} dB, spread {spread:.6f} dB")
    print(f"  vs recorded GTX 1660 {SHOOTOUT_GTX1660_SI_SDR} dB: delta {result['hardware_transfer']['delta_db']:+.3f} dB")
    print("\n  quoted deltas against the measured noise floor:")
    for k, v in verdict.items():
        print(f"    {k:>62}  {v}")

    OUT.write_text(json.dumps(result, indent=2) + "\n")
    print(f"\n[done] wrote {OUT}")


if __name__ == "__main__":
    main()
