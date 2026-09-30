# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = [
#   "torch==2.6.0",
#   "torchaudio==2.6.0",
#   "audio-separator[gpu]==0.44.5",
# ]
#
# [[tool.uv.index]]
# name = "pytorch-cu124"
# url = "https://download.pytorch.org/whl/cu124"
# explicit = true
#
# [tool.uv.sources]
# torch = { index = "pytorch-cu124" }
# torchaudio = { index = "pytorch-cu124" }
# ///
"""Separate JamendoLyrics vocal stems on a local CUDA GPU (the round-1 RoFormer config).

The aligner bench scores alignment on vocal stems, because that is what the
real pipeline aligns. Round 1 made those stems on Modal H100; this makes the
same stems on any local NVIDIA card so the bench needs no Modal spend.

Requirements
- ✔︎ Same separator config as round 1: audio-separator 0.44.5, Mel-Band RoFormer
  checkpoint DEFAULT_CHECKPOINT, overlap 8, segment 256 (scripts/modal_roformer_spike.py).
    [if] cuda is unavailable [then ⛔️] hard error before loading anything
    [if] the checkpoint yields no "(Vocals)" output [then ⛔️] that song errors
- ✔︎ Output <out_dir>/<name>-vocals.flac, one per dataset song; existing files are kept.
    [if] a vocals file already exists and --force is not set [then] skipped, named
    [if] any song failed [then] exit 1, with every failure listed
- ✔︎ A timing manifest <out_dir>/_separation.json (checkpoint, device, per-song seconds).

Usage (repo root, dataset from scripts/pull_jamendolyrics.py):
  uv run --script scripts/lyrics_alignment/gpu_separate_vocals.py \\
      --out-dir data/datasets/jamendolyrics-vocals-local

-Claude
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import time
from pathlib import Path

import torch
from audio_separator.separator import Separator

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "data" / "datasets" / "jamendolyrics"
CHECKPOINT = "model_mel_band_roformer_ep_3005_sdr_11.4360.ckpt"
MDXC_PARAMS = {
    "segment_size": 256,
    "override_model_segment_size": False,
    "batch_size": 1,
    "overlap": 8,
    "pitch_shift": 0,
}

# -----------------------------------------------------------------------------


def _song_names() -> list[str]:
    with (DATASET_DIR / "JamendoLyrics.csv").open(encoding="utf-8") as f:
        return [Path(r["Filepath"]).stem for r in csv.DictReader(f)]


def _separate_one(sep: Separator, staging: Path, name: str, out_path: Path) -> float:
    src = DATASET_DIR / "mp3" / f"{name}.mp3"
    if not src.resolve().is_file():
        raise FileNotFoundError(f"{src} missing")
    t0 = time.perf_counter()
    outputs = sep.separate(str(src.resolve()))
    took = time.perf_counter() - t0
    vocals = next((o for o in outputs if "(Vocals)" in o), None)
    if vocals is None:
        raise RuntimeError(f"{CHECKPOINT} produced no Vocals output: {outputs}")
    shutil.move(str(staging / Path(vocals).name), out_path)
    for other in outputs:
        (staging / Path(other).name).unlink(missing_ok=True)
    return took


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if not torch.cuda.is_available():
        raise SystemExit("[ERROR] cuda unavailable; this runner is GPU-only by design")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    names = _song_names()[: args.limit or None]

    staging = args.out_dir / "_staging"
    staging.mkdir(exist_ok=True)
    sep = Separator(output_dir=str(staging), output_format="FLAC", mdxc_params=MDXC_PARAMS)
    sep.load_model(CHECKPOINT)
    timings: dict[str, float] = {}
    failures: dict[str, str] = {}
    for name in names:
        out_path = args.out_dir / f"{name}-vocals.flac"
        if out_path.exists() and not args.force:
            print(f"[SKIP] {name} (exists)")
            continue
        try:
            timings[name] = round(_separate_one(sep, staging, name, out_path), 2)
            print(f"[ok] {name} {timings[name]}s", flush=True)
        except Exception as exc:  # per-song isolation; every failure is reported and fails the run
            failures[name] = f"{type(exc).__name__}: {exc}"
            print(f"[ERR] {name} {failures[name]}", flush=True)

    manifest = {
        "checkpoint": CHECKPOINT,
        "mdxc_params": MDXC_PARAMS,
        "device": torch.cuda.get_device_name(0),
        "seconds": timings,
        "failures": failures,
    }
    (args.out_dir / "_separation.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[total] {len(timings)} separated, {len(failures)} failed, {sum(timings.values()):.0f}s")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
