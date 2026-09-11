#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = [
#   "torch==2.5.1",
#   "torchaudio==2.5.1",
#   "soundfile>=0.12",
#   "numpy<2",
# ]
# ///
"""Candidate 3: torchaudio's own bundled Hybrid Demucs pipeline.

This is the "torchaudio hybrid" candidate the issue names: torchaudio ships a
pretrained Hybrid Demucs (same family as htdemucs) through
``torchaudio.pipelines.HDEMUCS_HIGH_MUSDB_PLUS`` -- no separate `demucs`
package, no Meta checkpoint download outside torchaudio's own hub cache. It
is evaluated as its own candidate (not assumed identical to
candidate_htdemucs_cpu.py) because the weights, the pre/post-processing and
the dependency footprint all differ: torchaudio-only vs. the `demucs` PyPI
package, which matters for "can I ship this without pulling in demucs".

Device is auto-detected the same way as the other candidates.

Run:
  uv run scripts/bench/local/candidate_torchaudio_hybrid_cpu.py \\
      --mixture path/to/mixture.wav --out .tmp/out/torchaudio_hybrid

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
import torchaudio

STEM_PARTS: tuple[str, ...] = ("drums", "bass", "other", "vocals")  # bundle's own order


def _pick_device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _peak_rss_mb() -> float:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / 1024 / 1024 if sys.platform == "darwin" else raw / 1024


def _sync_device(device: str) -> None:
    """Block until queued accelerator work completes. CUDA and MPS kernels launch
    asynchronously, so a perf_counter stopped right after enqueueing (rather than
    after the device finishes) undercounts load_s/separate_s for those devices."""
    if device == "cuda":
        torch.cuda.synchronize()
    elif device == "mps":
        torch.mps.synchronize()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mixture", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--device", default=None, help="override auto-detected device")
    args = parser.parse_args()


    device = args.device or _pick_device()
    args.out.mkdir(parents=True, exist_ok=True)

    bundle = torchaudio.pipelines.HDEMUCS_HIGH_MUSDB_PLUS

    print("[debug] fetching bundle model...", file=sys.stderr, flush=True)
    load_started = time.perf_counter()
    model = bundle.get_model()
    print(
        f"[debug] model loaded in {time.perf_counter() - load_started:.1f}s",
        file=sys.stderr,
        flush=True,
    )
    model.to(device)
    model.eval()
    _sync_device(device)
    model_sr = bundle.sample_rate
    load_s = time.perf_counter() - load_started

    audio, sr = sf.read(str(args.mixture), dtype="float32", always_2d=True)  # (frames, ch)
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    if sr != model_sr:
        raise RuntimeError(
            f"sample-rate mismatch: fixture is {sr} Hz, bundle expects {model_sr} Hz "
            "(refusing a silent resample)"
        )
    wav = torch.from_numpy(audio.T).unsqueeze(0).to(device)  # (1, ch, frames)
    ref = wav.mean(0)
    wav_norm = (wav - ref.mean()) / ref.std()

    separate_started = time.perf_counter()
    with torch.no_grad():
        # Whole-clip single segment: these fixtures are ~6.8s, well inside one
        # chunk, so the chunk/overlap-fade machinery the bundle example uses
        # for multi-minute tracks is not exercised here -- flagged in the
        # research doc rather than silently assumed equivalent.
        sources = model(wav_norm)[0]  # (stems, ch, frames)
    _sync_device(device)
    separate_s = time.perf_counter() - separate_started

    sources = sources * ref.std() + ref.mean()

    disk_bytes = 0
    for i, part in enumerate(STEM_PARTS):
        stem = sources[i].cpu().numpy().T  # (frames, ch)
        out_path = args.out / f"{part}.wav"
        sf.write(str(out_path), stem, sr, subtype="PCM_16")
        disk_bytes += out_path.stat().st_size

    result = {
        "candidate": "torchaudio_hybrid_demucs",
        "model": "HDEMUCS_HIGH_MUSDB_PLUS",
        "device": device,
        "torch_version": torch.__version__,
        "torchaudio_version": torchaudio.__version__,
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
