# /// script
# requires-python = ">=3.10"
# dependencies = ["fast-bss-eval>=0.1.4", "soundfile>=0.12", "numpy<2", "scipy>=1.10"]
# ///
"""Measure the 'dB lost' of the RoFormer/demucs MP3 companion stems vs their own FLAC.

For each vocals.mp3 / instrumental.mp3 written by mp3_companion_encode.py, decode
it back to PCM (ffmpeg), verify it is sample-aligned against the FLAC it was
encoded from (cross-correlation -- LAME's Xing/Info header lets ffmpeg strip
encoder delay+padding on decode, but this is verified, not assumed), then score
SI-SDR of the decoded mp3 against the FLAC (same fast_bss_eval backend as
si_sdr_score.py, so numbers are directly comparable to the rest of this repo's
SI-SDR reporting). Higher dB = less lost to the mp3 round-trip.

Fails fast (AlignmentError) if the cross-correlation peak is too low to trust --
per this project's brittle-fail-fast rule, a suspicious number must not be
silently reported as if trustworthy.

Usage:
    uv run scripts/bench/mp3_companion_sisdr.py \
        data/state/stems-roformer-spike data/state/stems-demucs-ab [--out results.json]

Requirements (mini-PRD)
  ✔︎ ✅ per-stem SI-SDR of decoded-mp3 vs its own flac, sample-aligned
    [if] decoded mp3 length == flac length and content matches [then] lag ~= 0,
      peak correlation > 0.999 (round-trip lossy noise only)
    [if] true misalignment exists (e.g. wrong pair compared) [then ⛔️] peak
      correlation drops well below MIN_ALIGNMENT_CORR and the run raises
  ✔︎ ✅ median across all scored stems reported
  ✔︎ ✅ JSON to stdout (or --out file) with per-stem + median, matching
    si_sdr_score.py's "JSON out, no prose" convention

-Claude
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import fast_bss_eval.numpy as fast_bss_eval_numpy
import numpy as np
import soundfile as sf
from scipy.signal import correlate, correlation_lags

STEMS: tuple[str, ...] = ("vocals", "instrumental")
MAX_LAG_SAMPLES = 8192  # +-186 ms @ 44.1kHz -- generous vs LAME's ~576-1152 sample encoder delay
MIN_ALIGNMENT_CORR = 0.9  # normalized cross-correlation peak required to trust the alignment


class AlignmentError(RuntimeError):
    pass


def _decode_mp3_to_wav(mp3_path: Path, wav_path: Path) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(mp3_path), str(wav_path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed for {mp3_path}: {proc.stderr.strip()}")


def _load_mono(path: Path) -> np.ndarray:
    data, _sr = sf.read(str(path), dtype="float64", always_2d=True)
    return data.mean(axis=1)


def _align(reference: np.ndarray, estimate: np.ndarray) -> tuple[np.ndarray, np.ndarray, int, float]:
    """Cross-correlate, verify the peak is trustworthy, then trim to the aligned overlap."""
    n = min(len(reference), len(estimate))
    a = reference[:n] - reference[:n].mean()
    b = estimate[:n] - estimate[:n].mean()
    corr = correlate(a, b, mode="full", method="fft")
    lags = correlation_lags(len(a), len(b), mode="full")
    mask = np.abs(lags) <= MAX_LAG_SAMPLES
    cm, lm = corr[mask], lags[mask]
    idx = int(np.argmax(np.abs(cm)))
    best_lag = int(lm[idx])
    norm = np.sqrt(np.sum(a**2) * np.sum(b**2))
    peak_corr = float(cm[idx] / norm) if norm > 0 else 0.0
    if peak_corr < MIN_ALIGNMENT_CORR:
        raise AlignmentError(
            f"cross-correlation peak {peak_corr:.4f} < {MIN_ALIGNMENT_CORR} threshold "
            f"(best_lag={best_lag}) -- cannot trust alignment, refusing to score SI-SDR"
        )
    if best_lag >= 0:
        aligned_ref = reference[best_lag:]
        aligned_est = estimate[: len(aligned_ref)]
    else:
        aligned_est = estimate[-best_lag:]
        aligned_ref = reference[: len(aligned_est)]
    m = min(len(aligned_ref), len(aligned_est))
    return aligned_ref[:m], aligned_est[:m], best_lag, peak_corr


def _si_sdr_db(reference: np.ndarray, estimate: np.ndarray) -> float:
    ref = reference[None, :]
    est = estimate[None, :]
    score = fast_bss_eval_numpy.si_sdr(ref, est)
    return round(float(np.asarray(score).reshape(-1)[0]), 3)


def _score_one(flac_path: Path, mp3_path: Path, tmp_dir: Path) -> dict:
    wav_path = tmp_dir / (mp3_path.stem + "_decoded.wav")
    _decode_mp3_to_wav(mp3_path, wav_path)
    reference = _load_mono(flac_path)
    estimate = _load_mono(wav_path)
    wav_path.unlink()
    aligned_ref, aligned_est, lag, peak_corr = _align(reference, estimate)
    si_sdr = _si_sdr_db(aligned_ref, aligned_est)
    return {
        "si_sdr_db": si_sdr,
        "alignment_lag_samples": lag,
        "alignment_peak_corr": round(peak_corr, 5),
        "n_samples_scored": int(min(len(aligned_ref), len(aligned_est))),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dirs", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    results: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="mp3-sisdr-") as tmp:
        tmp_dir = Path(tmp)
        for stems_dir in args.dirs:
            if not stems_dir.is_dir():
                raise SystemExit(f"not a directory: {stems_dir}")
            for track_dir in sorted(stems_dir.iterdir()):
                if not track_dir.is_dir():
                    continue
                for stem in STEMS:
                    flac_path = track_dir / f"{stem}.flac"
                    mp3_path = track_dir / f"{stem}.mp3"
                    if not flac_path.is_file() or not mp3_path.is_file():
                        continue
                    key = f"{stems_dir.name}/{track_dir.name}/{stem}"
                    info = _score_one(flac_path, mp3_path, tmp_dir)
                    results[key] = info
                    print(
                        f"{key:<75} si_sdr={info['si_sdr_db']:>8.3f} dB  "
                        f"lag={info['alignment_lag_samples']:>5}  corr={info['alignment_peak_corr']:.5f}",
                        file=sys.stderr,
                    )

    values = [v["si_sdr_db"] for v in results.values()]
    median = round(float(np.median(values)), 3) if values else None
    output = {"results": results, "median_si_sdr_db": median, "n": len(values)}
    print(f"\nmedian SI-SDR (mp3 vs flac, n={len(values)}): {median} dB", file=sys.stderr)

    text = json.dumps(output, indent=2)
    if args.out:
        args.out.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
