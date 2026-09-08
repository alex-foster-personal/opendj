#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = [
#   "demucs==4.0.1",
#   "torch==2.5.1",
#   "torchaudio==2.5.1",
#   "soundfile>=0.12",
#   "numpy<2",
# ]
# ///
"""Candidate 1: demucs htdemucs, on whatever accelerator this machine has.

Same model and pinned versions as apps/stems/tiers.py's LOCAL rung and
scripts/stem_bundle_worker.py, so this candidate's numbers are directly
comparable to that rung's existing evidence, not a parallel reinvention of it.

Device is AUTO-DETECTED (cuda > cpu) and reported in the output JSON. htdemucs
stays on CPU on Apple Silicon because the production LOCAL worker rejects MPS
for its output-channel limitation. No candidate here fabricates an MPS/CoreML
number: this candidate reports CPU on an Air and nucbox unless CUDA is real.

Run:
  uv run scripts/bench/local/candidate_htdemucs_cpu.py \\
      --mixture path/to/mixture.wav --out .tmp/out/htdemucs

-Claude
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

MODEL_NAME = "htdemucs"
STEM_PARTS: tuple[str, ...] = ("drums", "bass", "other", "vocals")  # demucs' own order


def _pick_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _peak_rss_mb() -> float:
    # ru_maxrss is KB on Linux, bytes on macOS -- normalise both to MB.
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / 1024 / 1024 if sys.platform == "darwin" else raw / 1024


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mixture", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--device", default=None, help="override auto-detected device")
    args = parser.parse_args()

    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    device = args.device or _pick_device()
    args.out.mkdir(parents=True, exist_ok=True)

    load_started = time.perf_counter()
    model = get_model(MODEL_NAME)
    model.to(device)
    model.eval()
    load_s = time.perf_counter() - load_started

    audio, sr = sf.read(str(args.mixture), dtype="float32", always_2d=True)  # (frames, ch)
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    wav = torch.from_numpy(audio.T).unsqueeze(0).to(device)  # (1, ch, frames)
    reference = wav.mean(0)
    wav_normalized = (wav - reference.mean()) / reference.std()

    separate_started = time.perf_counter()
    with torch.no_grad():
        sources = apply_model(model, wav_normalized, device=device, progress=False)[0]
    separate_s = time.perf_counter() - separate_started
    sources = sources * reference.std() + reference.mean()

    disk_bytes = 0
    for i, part in enumerate(STEM_PARTS):
        stem = sources[i].cpu().numpy().T  # (frames, ch)
        out_path = args.out / f"{part}.wav"
        sf.write(str(out_path), stem, sr, subtype="PCM_16")
        disk_bytes += out_path.stat().st_size

    result = {
        "candidate": "htdemucs_torch",
        "model": MODEL_NAME,
        "device": device,
        "torch_version": torch.__version__,
        "load_s": round(load_s, 3),
        "separate_s": round(separate_s, 3),
        "wall_s": round(load_s + separate_s, 3),
        "peak_rss_mb": round(_peak_rss_mb(), 1),
        "disk_bytes_4_stems": disk_bytes,
        "mixture": str(args.mixture),
        "out_dir": str(args.out),
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
