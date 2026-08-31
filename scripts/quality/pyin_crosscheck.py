"""Cross-check the proposed numpy YIN tracker against librosa pyin.

Amendment 8 leaves the tracker choice to the lanes to PROPOSE. This is the
evidence for the proposal: the same pitched excerpts, tracked by both, agreed
in cents. Run it, do not assume it.

    uv run --with librosa --no-sync python -m scripts.quality.pyin_crosscheck \
        --excerpts ops/quality/stretch/work/excerpts --fixtures F2 F5

``--with`` puts librosa in an EPHEMERAL OVERLAY environment. It never enters
the repo venv, which is the whole reason the analysis half is numpy-only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from scripts.quality import metrics


def compare_one(path: Path, hop_samples: int) -> dict[str, object]:
    import librosa

    samples, meta = metrics.read_pcm(path)
    mono = metrics.to_mono(samples)
    mine, _ = metrics.track_f0(mono, meta.sample_rate_hz, hop_samples)

    theirs, voiced_flag, _ = librosa.pyin(
        mono.astype(np.float32),
        sr=meta.sample_rate_hz,
        fmin=metrics.PITCH_MIN_HZ,
        fmax=metrics.PITCH_MAX_HZ,
        frame_length=metrics.PITCH_FRAME_SAMPLES,
        hop_length=hop_samples,
    )

    frames = min(len(mine), len(theirs))
    mine, theirs = mine[:frames], theirs[:frames]
    both = np.isfinite(mine) & np.isfinite(theirs) & (voiced_flag[:frames])
    if not both.any():
        return {"fixture": path.stem, "status": "NO_JOINTLY_VOICED_FRAMES"}

    cents = 1200.0 * np.log2(mine[both] / theirs[both])
    # An octave disagreement is a different failure from a tuning disagreement,
    # so it is counted separately rather than blurred into the percentiles.
    octave_apart = int(np.sum(np.abs(np.abs(cents) - 1200.0) < 100.0))
    close = np.abs(cents) < 600.0
    return {
        "fixture": path.stem,
        "status": "ok",
        "frames_compared": int(both.sum()),
        "voiced_fraction_mine": float(np.isfinite(mine).mean()),
        "voiced_fraction_pyin": float(np.isfinite(theirs).mean()),
        "octave_disagreements": octave_apart,
        "median_cents_offset": float(np.median(cents[close])) if close.any() else float("nan"),
        "p95_abs_cents": float(np.percentile(np.abs(cents[close]), 95))
        if close.any()
        else float("nan"),
        "agreement_within_20_cents": float(np.mean(np.abs(cents[close]) < 20.0))
        if close.any()
        else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--excerpts", type=Path, required=True)
    parser.add_argument("--fixtures", nargs="+", default=["F2", "F5"])
    parser.add_argument("--hop", type=int, default=metrics.PITCH_HOP_SAMPLES)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    results = []
    for fixture in args.fixtures:
        path = args.excerpts / f"{fixture}.f32"
        if not path.exists():
            print(f"[MISSING] {fixture}: {path} not rendered yet")
            results.append({"fixture": fixture, "status": "MISSING"})
            continue
        result = compare_one(path, args.hop)
        results.append(result)
        if result["status"] != "ok":
            print(f"[{result['status']}] {fixture}")
            continue
        print(
            f"[{fixture}] {result['frames_compared']} frames jointly voiced; "
            f"median offset {result['median_cents_offset']:+.2f} cents, "
            f"p95 {result['p95_abs_cents']:.2f} cents, "
            f"{result['agreement_within_20_cents']:.1%} within 20 cents, "
            f"{result['octave_disagreements']} octave disagreements"
        )

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"[out] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
