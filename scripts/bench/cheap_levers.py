# /// script
# requires-python = ">=3.11"
# dependencies = ["torch", "demucs", "diffq", "numpy<2", "soundfile", "scipy"]
# ///
"""Measure the cheap levers for vocal separation, one at a time, on one machine.

Model choice is only one knob. This measures the ones we had never varied:
precision, segment length, separation sample rate, mono folding, and overlap.
Every arm writes vocals.wav + instrumental.wav at the ORIGINAL rate and channel
count so all arms are scored and auditioned on equal terms, and reports
seconds per stem-minute of inference (model load excluded, as in the shootout).

Reduced-rate arms resample down, separate, and resample back up. That is the
honest accounting: the resample cost is part of the arm's cost.
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from demucs.apply import apply_model
from demucs.pretrained import get_model
from scipy.signal import resample_poly

SOURCES_WANTED = ("vocals",)


def _load(path: Path) -> tuple[torch.Tensor, int]:
    audio, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return torch.from_numpy(audio.T), sr


def _resample(x: torch.Tensor, src: int, dst: int) -> torch.Tensor:
    if src == dst:
        return x
    g = np.gcd(src, dst)
    return torch.from_numpy(
        resample_poly(x.numpy(), dst // g, src // g, axis=-1).astype(np.float32))


@dataclass(frozen=True)
class SepRunOptions:
    overlap: float
    segment: float | None
    sep_rate: int
    mono: bool
    dtype: str
    device: str
    jobs: int


def run_arm(
    model_name: str,
    mix: torch.Tensor,
    sr: int,
    opts: SepRunOptions,
) -> tuple[torch.Tensor, torch.Tensor, float]:
    """Return (vocals, instrumental, inference seconds), both at the input rate."""
    model = get_model(model_name)
    model.eval()

    work = _resample(mix, sr, opts.sep_rate)
    n_in = work.shape[-1]
    if opts.mono:
        work = work.mean(dim=0, keepdim=True).repeat(2, 1)

    # Casting the whole model to half fails: demucs runs torch.stft internally and
    # that has no half kernel. autocast is the working form of the precision lever,
    # keeping the transforms in fp32 and the convolutions in low precision.
    autocast_dtype = {"fp32": None, "fp16": torch.float16,
                      "bf16": torch.bfloat16}[opts.dtype]
    model.to(device=opts.device)
    batch = work.unsqueeze(0).to(device=opts.device)

    kwargs = {"overlap": opts.overlap, "shifts": 0, "split": True,
              "progress": False, "device": opts.device, "num_workers": opts.jobs}
    if opts.segment is not None:
        kwargs["segment"] = opts.segment

    t0 = time.perf_counter()
    with torch.no_grad():
        if autocast_dtype is None:
            out = apply_model(model, batch, **kwargs)[0]
        else:
            with torch.autocast(device_type=device, dtype=autocast_dtype):
                out = apply_model(model, batch, **kwargs)[0]
    infer_s = time.perf_counter() - t0

    out = out.to("cpu", dtype=torch.float32)
    idx = model.sources.index("vocals")
    vocals = out[idx]
    inst = out.sum(dim=0) - vocals

    if mono:
        # Restore the original stereo image by scaling the mixture, so a mono arm
        # is judged on its separation, not on having been collapsed to mono.
        vocals = vocals.mean(dim=0, keepdim=True).repeat(2, 1)
        inst = inst.mean(dim=0, keepdim=True).repeat(2, 1)
    vocals = _resample(vocals[..., :n_in], sep_rate, sr)
    inst = _resample(inst[..., :n_in], sep_rate, sr)
    n = mix.shape[-1]
    return vocals[..., :n], inst[..., :n], infer_s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--model", default="htdemucs")
    ap.add_argument("--overlap", type=float, default=0.25)
    ap.add_argument("--segment", type=float, default=None)
    ap.add_argument("--sep-rate", type=int, default=44100)
    ap.add_argument("--mono", action="store_true")
    ap.add_argument("--dtype", default="fp32", choices=["fp32", "fp16", "bf16"])
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--jobs", type=int, default=0)
    ap.add_argument("--label", required=True)
    args = ap.parse_args()

    mix, sr = _load(args.input)
    opts = SepRunOptions(
        overlap=args.overlap,
        segment=args.segment,
        sep_rate=args.sep_rate,
        mono=args.mono,
        dtype=args.dtype,
        device=args.device,
        jobs=args.jobs,
    )
    vocals, inst, infer_s = run_arm(args.model, mix, sr, opts)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    sf.write(str(args.out_dir / "vocals.wav"), vocals.T.numpy(), sr)
    sf.write(str(args.out_dir / "instrumental.wav"), inst.T.numpy(), sr)

    dur = mix.shape[-1] / sr
    print(json.dumps({
        "label": args.label, "model": args.model, "overlap": args.overlap,
        "segment": args.segment, "sep_rate": args.sep_rate, "mono": args.mono,
        "dtype": args.dtype, "device": args.device,
        "audio_s": round(dur, 2), "infer_s": round(infer_s, 2),
        "s_per_stem_minute": round(infer_s / (dur / 60), 2),
    }))


if __name__ == "__main__":
    main()
