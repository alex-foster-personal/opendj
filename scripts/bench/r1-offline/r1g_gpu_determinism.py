# /// script
# requires-python = ">=3.11"
# dependencies = ["torch", "demucs", "numpy<2", "soundfile"]
# ///
"""R1g: GPU run-to-run determinism for demucs, run on the GTX 1660 the shootout used.

Companion to r1f_determinism.py, which established that the CPU path is bit-exact across
separate processes. GPU is the likelier nondeterminism source: cuDNN can autotune and pick
different kernels between runs, and the shootout doc already records a first-run kernel
selection effect inflating one timing by roughly 4x.

Takes a pre-windowed mixture and truth vocal so no window arithmetic can drift between the
two machines. Separates N times in SEPARATE PROCESSES and reports SI-SDR plus a hash of
the SAMPLE DATA (never the file: libsndfile writes a wall-clock timestamp into the WAV
PEAK chunk, which reads as nondeterminism when it is just a clock).

Run:
  uv run r1g_gpu_determinism.py --mix mix.wav --truth truth.wav --runs 3
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

MODEL = "hdemucs_mmi"
OVERLAP = 0.25


def si_sdr_db(reference: np.ndarray, estimate: np.ndarray) -> float:
    n = min(len(reference), len(estimate))
    ref, est = reference[:n].astype(np.float64), estimate[:n].astype(np.float64)
    ref, est = ref - ref.mean(), est - est.mean()
    alpha = float(np.dot(est, ref) / np.dot(ref, ref))
    target = alpha * ref
    noise = est - target
    return float(10.0 * np.log10(np.dot(target, target) / max(np.dot(noise, noise), 1e-30)))


def worker(mix_path: Path, out_path: Path, device: str) -> None:
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    mix, sr = sf.read(str(mix_path), dtype="float32", always_2d=True)
    model = get_model(MODEL)
    model.eval()
    tensor = torch.from_numpy(mix.T).unsqueeze(0)
    started = time.perf_counter()
    with torch.no_grad():
        out = apply_model(model, tensor, overlap=OVERLAP, shifts=0, split=True,
                          progress=False, device=device, num_workers=0)[0]
    elapsed = time.perf_counter() - started
    vocals = out[model.sources.index("vocals")].cpu().numpy().T
    sf.write(str(out_path), vocals, sr, subtype="FLOAT")
    print(json.dumps({"separate_s": round(elapsed, 3), "device": device}))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mix", type=Path, required=True)
    p.add_argument("--truth", type=Path)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--device", default="cuda")
    p.add_argument("--worker", action="store_true")
    p.add_argument("--out", type=Path)
    a = p.parse_args()

    if a.worker:
        worker(a.mix, a.out, a.device)
        return

    import torch
    print(json.dumps({"cuda_available": torch.cuda.is_available(),
                      "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
                      "torch": torch.__version__}))
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA not available; refusing to report a GPU result from the CPU path")

    truth, _ = sf.read(str(a.truth), dtype="float32", always_2d=True)
    truth_mono = truth.mean(axis=1)
    runs = []
    for i in range(a.runs):
        out_path = Path(f"r1g_run{i}.wav")
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker", "--mix", str(a.mix),
             "--out", str(out_path), "--device", a.device],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"run {i} failed:\n{proc.stdout}\n{proc.stderr}")
        meta = json.loads(proc.stdout.strip().splitlines()[-1])
        est, _ = sf.read(str(out_path), dtype="float32", always_2d=True)
        runs.append({"run": i, "si_sdr": round(si_sdr_db(truth_mono, est.mean(axis=1)), 6),
                     "sample_sha256": hashlib.sha256(est.tobytes()).hexdigest()[:16],
                     "separate_s": meta["separate_s"]})
        print(json.dumps(runs[-1]))

    scores = [r["si_sdr"] for r in runs]
    print(json.dumps({
        "n_runs": len(runs),
        "bit_identical": len({r["sample_sha256"] for r in runs}) == 1,
        "si_sdr_mean": round(statistics.mean(scores), 6),
        "si_sdr_stdev": round(statistics.stdev(scores), 6),
        "si_sdr_spread": round(max(scores) - min(scores), 6),
    }))


if __name__ == "__main__":
    main()
