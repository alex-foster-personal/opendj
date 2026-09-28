#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "torch==2.7.1",
#   "torchaudio==2.7.1",
#   "onnxscript==0.7.1",
#   "skey @ git+https://github.com/deezer/skey.git@918b83d273568d5041569bb8068843d19a335726",
# ]
#
# [tool.uv.sources]
# torch = [
#   { index = "pytorch-cpu", marker = "sys_platform == 'linux'" },
#   { index = "pytorch-cu126", marker = "sys_platform == 'win32'" },
# ]
# torchaudio = [
#   { index = "pytorch-cpu", marker = "sys_platform == 'linux'" },
#   { index = "pytorch-cu126", marker = "sys_platform == 'win32'" },
# ]
#
# [[tool.uv.index]]
# name = "pytorch-cpu"
# url = "https://download.pytorch.org/whl/cpu"
# explicit = true
#
# [[tool.uv.index]]
# name = "pytorch-cu126"
# url = "https://download.pytorch.org/whl/cu126"
# explicit = true
# ///
"""Deezer S-KEY (ICASSP 2025) as a candidate for nav1-key-r0 (issue #1478).

A PEP 723 SCRIPT, NOT A REPO MODULE. torch, torchaudio, and S-KEY's weights
never enter the repo venv (CLAUDE.md): this file imports nothing from
apps/, so it stays runnable in its own throwaway `uv run` environment.
It writes plain JSON so the repo-venv side (a future bench scorer under
apps/analysis_key/) never needs S-KEY's dependency tree.

EVERY INFERENCE DEPENDENCY IS PINNED EXACTLY (torch, torchaudio, onnxscript,
and skey itself by commit), matching apps/analysis_beatgrid/beat_this_runner.py's
reproducibility contract: a range would let a future release change decoding,
inference or export behavior while this file's PRODUCER_VERSION stayed still,
so two runs claiming the same producer version could silently differ. The
pinned torch/torchaudio/onnxscript versions are what `uv` actually resolves
together with the pinned skey commit (not chosen independently), and all
three plus skey_revision are stamped into every output payload.

WINDOWS NEEDS ITS OWN CUDA INDEX, cu126: without an explicit override,
`torch`/`torchaudio` resolve to whatever default PyPI publishes for win32,
which on a CUDA-equipped Windows box is CPU-only -- never the acceleration
that box actually has (mirroring apps/analysis_beatgrid/beat_this_runner.py's
own linux pytorch-cpu pin, in the opposite direction). `pytorch-cu126`
carries torch==2.7.1 and torchaudio==2.7.1 win_amd64 wheels at the exact
pinned versions (confirmed against the index directly, not inferred);
`pytorch-cu124` does not and was never committed here. `uv lock --script`
resolves cleanly with this header and no requires-python cap.

DEVICE: `auto` here means "prefer a real accelerator", mirroring
apps/analysis_beatgrid/beat_this_runner.py's convention. CPU is the
REQUIRED path (specs/native-analysis-v1.md section 4, "CPU-only path"), so
`--device cpu` must always work, and CPU is the only device this file's own
timing claims cover. Measured on this box (CPU, one 5 s synthetic tone,
including checkpoint load): 3.766 s wall time -- NOT a per-track figure once
a model is warm, since checkpoint load dominates a single short run. Rerun
with `--device cpu` over a real fixture set for a warm per-track number.

CHECKPOINT LICENSE: SKEY's LICENSE file and README both say "The code of
SKEY is MIT-licensed" -- scoped language that names the CODE, not the
weights. The checkpoint (skey/models/skey.pt, shipped inside the pip/git
package, not a separate download) has no license statement of its own
anywhere in the repository. `--out` records size and sha256 of the exact
file loaded so this is a checkable claim, not a remembered one, and the
license field is reported as "code MIT-licensed per README; weights
license UNSPECIFIED" rather than assumed permissive.

ONNX EXPORT: attempted, not assumed. skey.chromanet.convnext's
TimeDownsamplingBlock computes `nn.functional.layer_norm(x, x.shape[1:])`,
reading the input's shape at trace time; both the legacy tracer (dynamo=
False) and the newer dynamo=True exporter are attempted and BOTH exact
failures (or successes) are recorded, since the two exporters can behave
differently and reporting only one is a materially weaker artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import onnxscript
import torch
import torchaudio
from skey.key_detection import (
    DEFAULT_CHECKPOINT_PATH,
    load_audio,
    load_checkpoint,
    load_model_components,
)

logging.getLogger().setLevel(logging.WARNING)  # skey's own basicConfig is chatty at INFO

PRODUCER = "skey.icassp2025"
PRODUCER_VERSION = "0.1.0"  # skey has no PyPI release; SKEY_REVISION is the real pin
# The exact commit pinned in this file's [tool.uv.sources] dependency line above.
# Recorded in the output payload so a result is never attributable to whatever
# skey happened to be at HEAD on the day it ran.
SKEY_REVISION = "918b83d273568d5041569bb8068843d19a335726"


#-----------------------------------------------------------------------------
def resolve_device(requested: str) -> str:
    """`auto` picks mps, else cuda, else cpu. Anything else is taken literally.

    An explicit `--device mps`/`--device cuda` on a machine without it is an
    error, not a silent fall back: a timing figure attributed to the wrong
    device is worse than a crash. `skey.detect_key` has its own internal
    cpu-fallback for "auto"-like leniency; this is stricter on purpose so a
    round-0 timing table is never silently mislabeled.
    """
    if requested != "auto":
        if requested == "mps" and not torch.backends.mps.is_available():
            raise SystemExit("[skey] --device mps requested but MPS is not available")
        if requested == "cuda" and not torch.cuda.is_available():
            raise SystemExit("[skey] --device cuda requested but CUDA is not available")
        return requested
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _checkpoint_provenance(checkpoint_path: str | Path) -> dict[str, Any]:
    path = Path(checkpoint_path)
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
        "license": "code MIT-licensed per skey README/LICENSE; weights license UNSPECIFIED",
    }


def _skey_label_to_rekordbox_style(label: str) -> str:
    """"F# Major" -> "F#", "A minor" -> "Am"; matches the spelling
    apps.analysis_key.canon.from_rekordbox_scale_name already accepts, so a
    downstream scorer needs no S-KEY-specific parsing. Raises on skey's own
    "error" sentinel (see infer_key's except branch) rather than mapping it
    to a fabricated key.
    """
    if label.endswith(" Major"):
        return label[: -len(" Major")]
    if label.endswith(" minor"):
        return label[: -len(" minor")] + "m"
    raise ValueError(f"not a skey key_map label: {label!r}")


#-----------------------------------------------------------------------------
def analyze_one(
    path: str, hcqt, chromanet, crop_fn, sample_rate: int, device: torch.device
) -> dict[str, Any]:
    from skey.key_detection import infer_key

    started = time.time()
    waveform = load_audio(path, sample_rate).to(device)
    label = infer_key(hcqt, chromanet, crop_fn, waveform, device)
    elapsed_s = round(time.time() - started, 3)
    if label == "error":
        return {
            "audio": path,
            "label": None,
            "key_rekordbox_style": None,
            "inference_s": elapsed_s,
            "error": "skey infer_key returned its own 'error' sentinel (likely audio too short)",
        }
    return {
        "audio": path,
        "label": label,
        "key_rekordbox_style": _skey_label_to_rekordbox_style(label),
        "inference_s": elapsed_s,
        "error": None,
    }


#-----------------------------------------------------------------------------
def attempt_onnx_export(
    hcqt, chromanet, crop_fn, sample_rate: int, device: torch.device, dynamo: bool
) -> dict[str, Any]:
    """One export attempt, one exact result. Never assumed successful."""

    class _FullPipeline(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.hcqt = hcqt
            self.chromanet = chromanet
            self.crop_fn = crop_fn

        def forward(self, audio: torch.Tensor) -> torch.Tensor:
            cqt = self.hcqt(audio)
            cropped = self.crop_fn(cqt, torch.zeros(1))
            return self.chromanet(cropped)

    pipeline = _FullPipeline().to(device).eval()
    dummy = torch.zeros(1, 1, sample_rate * 15, dtype=torch.float32, device=device)

    with tempfile.TemporaryDirectory() as tmp:
        onnx_path = os.path.join(tmp, "skey.onnx")
        try:
            export_kwargs: dict[str, Any] = {
                "input_names": ["audio"],
                "output_names": ["logits"],
                "dynamo": dynamo,
            }
            if not dynamo:
                export_kwargs["opset_version"] = 17
            torch.onnx.export(pipeline, (dummy,), onnx_path, **export_kwargs)
        except Exception as exc:  # noqa: BLE001 - the exact exception IS the deliverable
            error = f"{type(exc).__name__}: {exc}"[:2000]
            return {"dynamo": dynamo, "success": False, "error": error}
        else:
            return {"dynamo": dynamo, "success": True, "error": None}


#-----------------------------------------------------------------------------
def main() -> int:
    # Windows consoles default to cp1252 (specs/native-analysis-v1.md's
    # "Console encoding" rule); reconfigure so a non-ASCII track title in
    # --audio or --out cannot raise UnicodeEncodeError mid-run.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audio", nargs="+", required=True, help="audio file(s) to analyze")
    ap.add_argument("--out", required=True, help="JSON output path")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "mps", "cuda"])
    ap.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT_PATH))
    ap.add_argument(
        "--skip-onnx-export", action="store_true", help="skip the (slow) export attempts"
    )
    args = ap.parse_args()

    device_name = resolve_device(args.device)
    device = torch.device(device_name)
    print(f"[skey] device={device_name} checkpoint={args.checkpoint}", flush=True)

    load_started = time.time()
    ckpt = load_checkpoint(args.checkpoint)
    sample_rate = int(ckpt["audio"]["sr"])
    hcqt, chromanet, crop_fn = load_model_components(ckpt, device)
    load_s = round(time.time() - load_started, 3)
    checkpoint = _checkpoint_provenance(args.checkpoint)
    print(
        f"[skey] model loaded in {load_s}s, sr={sample_rate}, "
        f"checkpoint sha256={checkpoint['sha256'][:16]}...",
        flush=True,
    )

    results: dict[str, Any] = {}
    for path in args.audio:
        try:
            results[path] = analyze_one(path, hcqt, chromanet, crop_fn, sample_rate, device)
        except Exception as exc:  # noqa: BLE001 - a per-track failure is a result, not an abort
            error = f"{type(exc).__name__}: {exc}"[:300]
            results[path] = {
                "audio": path,
                "label": None,
                "key_rekordbox_style": None,
                "inference_s": None,
                "error": error,
            }
            print(f"[skey] FAILED {path}: {error}", flush=True)

    onnx_export: list[dict[str, Any]] = []
    if not args.skip_onnx_export:
        for dynamo in (False, True):
            print(f"[skey] attempting torch.onnx.export(dynamo={dynamo})...", flush=True)
            result = attempt_onnx_export(hcqt, chromanet, crop_fn, sample_rate, device, dynamo)
            onnx_export.append(result)
            outcome = "OK" if result["success"] else result["error"]
            print(f"[skey]   dynamo={dynamo}: {outcome}", flush=True)

    payload = {
        "schema": 1,
        "producer": PRODUCER,
        "producer_version": PRODUCER_VERSION,
        "skey_revision": SKEY_REVISION,
        "torch_version": torch.__version__,
        "torchaudio_version": torchaudio.__version__,
        "onnxscript_version": onnxscript.__version__,
        "checkpoint": checkpoint,
        "device": device_name,
        "sample_rate": sample_rate,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model_load_s": load_s,
        "onnx_export": onnx_export,
        "n_tracks": len(args.audio),
        "n_failed": sum(1 for r in results.values() if r.get("error")),
        "results": results,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    print(f"[skey] done -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
