# /// script
# requires-python = ">=3.10"
# dependencies = ["fast-bss-eval>=0.1.4", "soundfile>=0.12", "numpy<2", "scipy>=1.10", "packaging>=23"]
# ///
"""Full separation metrics for one track: SI-SDR plus the under-separation checks.

scripts/bench/si_sdr_score.py answers "how close is the estimate to the truth"
and nothing else. SI-SDR alone is gameable: a separator that barely separates,
leaving the vocal buried in accompaniment, can still post a respectable number
because the vocal it kept is undistorted. This script adds the evidence needed
to catch that, scoring each estimate against the TRUE vocal stem AND against the
mixture it came from:

  si_sdr             primary quality vs the true vocal stem (higher = better)
  sir_db             accompaniment suppression from BSS Eval v4 with both true
                     sources supplied; the direct under-separation measure
  sar_db             artifact level (a shredded estimate scores low here)
  sdr_db             BSS Eval v4 SDR, the filtered cousin of si_sdr
  si_sdr_to_mixture  how well the estimate reconstructs the MIXTURE; a model
                     that passes the mixture through scores high here
  corr_mixture       |Pearson r| of estimate against the mixture, same idea
                     without the projection maths
  corr_truth         |Pearson r| of estimate against the true vocals
  lsd_db             log-spectral distance vs truth after scale alignment, a
                     cheap perceptual proxy (lower = better)

Also reports the do-nothing baseline (the mixture scored as if it were a vocal
estimate). Any model that fails to clear it by a wide margin is not separating.

Instrumental complements are found by convention: <estimate>.flac pairs with
<estimate>.inst.flac. Both are required, because SIR/SAR need the estimated
accompaniment as well as the estimated vocal.

Requirements (mini-PRD):
  ✔︎ ✅ every metric is computed against files that exist and share a sample
    rate; no hidden resample, no silent skip.
    [if] an estimate's .inst.flac is absent [then ⛔️] RuntimeError naming it
    [if] a sample rate differs from the reference [then ⛔️] RuntimeError
  ✔︎ ✅ SIR is computed with BOTH true sources present (vocals + accompaniment
    derived as mixture minus vocals), so it measures real leakage.
    [if] the mixture and truth lengths disagree by more than a sample [then]
      they are min-length aligned before subtraction, never zero-padded
  ✔︎ ✅ prints one JSON object to stdout: {estimate_path: {metrics}} plus a
    "_baseline" entry carrying the mixture-as-estimate scores.
    [if] stdout is not parseable JSON [then ⛔️] the caller raises

Run:
  uv run scripts/bench/separation_metrics.py --mixture mix.flac \
      --truth truth-vocals.flac --estimates a.flac b.flac

-Claude
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import fast_bss_eval.numpy as fast_bss_eval_numpy  # torch-free backend
import numpy as np
import soundfile as sf
from scipy import signal

# BSS Eval v4 distortion filter length. 512 taps at 44.1 kHz is the fast_bss_eval
# default and the value the SiSEC campaigns use, so numbers stay comparable.
FILTER_LENGTH: int = 512
# STFT for the log-spectral distance proxy.
LSD_NFFT: int = 2048
LSD_HOP: int = 512
# Power-spectrum floor, 100 dB below unity, so silent bins cannot dominate LSD.
LSD_FLOOR: float = 1e-10


# ----- loading ----------------------------------------------------------------


def _load_mono(path: Path) -> tuple[np.ndarray, int]:
    if not path.is_file():
        raise RuntimeError(f"audio file does not exist: {path}")
    data, sr = sf.read(str(path), dtype="float64", always_2d=True)
    return data.mean(axis=1), int(sr)


def _inst_path(vocal_path: Path) -> Path:
    """Convention from modal_vocal_ladder._write_clips: X.flac -> X.inst.flac."""
    inst = vocal_path.with_suffix("").with_suffix(".inst.flac")
    if inst == vocal_path or not inst.is_file():
        inst = Path(str(vocal_path)[: -len(vocal_path.suffix)] + ".inst.flac")
    if not inst.is_file():
        raise RuntimeError(
            f"instrumental complement missing for {vocal_path} (expected {inst}); "
            "SIR/SAR cannot be computed without the estimated accompaniment"
        )
    return inst


def _align(*signals: np.ndarray) -> list[np.ndarray]:
    n = min(len(s) for s in signals)
    if n == 0:
        raise RuntimeError("zero-length overlap between the supplied signals")
    return [s[:n] for s in signals]


# ----- metrics ----------------------------------------------------------------


def si_sdr_db(reference: np.ndarray, estimate: np.ndarray) -> float:
    ref, est = _align(reference, estimate)
    score = fast_bss_eval_numpy.si_sdr(ref[None, :], est[None, :])
    return round(float(np.asarray(score).reshape(-1)[0]), 3)


def bss_eval_two_source(
    true_vocals: np.ndarray,
    true_accomp: np.ndarray,
    est_vocals: np.ndarray,
    est_accomp: np.ndarray,
) -> dict[str, float]:
    """SDR/SIR/SAR for the vocal source with both true sources supplied.

    SIR is the number that exposes under-separation: it is the ratio of the
    target vocal to the accompaniment that leaked through, so a model that
    hands back something close to the mixture scores near 0 dB no matter how
    flattering its SI-SDR looks.
    """
    ref_v, ref_a, est_v, est_a = _align(true_vocals, true_accomp, est_vocals, est_accomp)
    reference = np.stack([ref_v, ref_a])
    estimate = np.stack([est_v, est_a])
    sdr, sir, sar = fast_bss_eval_numpy.bss_eval_sources(
        reference,
        estimate,
        filter_length=FILTER_LENGTH,
        compute_permutation=False,
    )[:3]
    return {
        "sdr_db": round(float(np.asarray(sdr).reshape(-1)[0]), 3),
        "sir_db": round(float(np.asarray(sir).reshape(-1)[0]), 3),
        "sar_db": round(float(np.asarray(sar).reshape(-1)[0]), 3),
    }


def abs_correlation(a: np.ndarray, b: np.ndarray) -> float:
    """|Pearson r| at zero lag. 1.0 means the two waveforms are the same shape."""
    x, y = _align(a, b)
    x = x - x.mean()
    y = y - y.mean()
    denominator = float(np.sqrt((x**2).sum() * (y**2).sum()))
    if denominator <= 0:
        raise RuntimeError("correlation undefined: one signal is digital silence")
    return round(abs(float((x * y).sum()) / denominator), 4)


def log_spectral_distance_db(reference: np.ndarray, estimate: np.ndarray) -> float:
    """Scale-aligned LSD in dB, lower is better.

    LSD is not scale invariant and the clips are peak-normalised inconsistently,
    so the estimate is first projected onto the reference (the same least-squares
    gain SI-SDR uses) before the spectra are compared.
    """
    ref, est = _align(reference, estimate)
    energy = float((est**2).sum())
    if energy <= 0:
        raise RuntimeError("estimate is digital silence, LSD undefined")
    est = est * (float((ref * est).sum()) / energy)
    _f, _t, ref_stft = signal.stft(ref, nperseg=LSD_NFFT, noverlap=LSD_NFFT - LSD_HOP)
    _f, _t, est_stft = signal.stft(est, nperseg=LSD_NFFT, noverlap=LSD_NFFT - LSD_HOP)
    ref_db = 10.0 * np.log10(np.maximum(np.abs(ref_stft) ** 2, LSD_FLOOR))
    est_db = 10.0 * np.log10(np.maximum(np.abs(est_stft) ** 2, LSD_FLOOR))
    per_frame = np.sqrt(((ref_db - est_db) ** 2).mean(axis=0))
    return round(float(per_frame.mean()), 3)


def score_estimate(
    truth: np.ndarray,
    accompaniment: np.ndarray,
    mixture: np.ndarray,
    est_vocals: np.ndarray,
    est_accomp: np.ndarray,
) -> dict[str, float]:
    metrics = {
        "si_sdr": si_sdr_db(truth, est_vocals),
        "si_sdr_to_mixture": si_sdr_db(mixture, est_vocals),
        "corr_truth": abs_correlation(truth, est_vocals),
        "corr_mixture": abs_correlation(mixture, est_vocals),
        "lsd_db": log_spectral_distance_db(truth, est_vocals),
    }
    metrics.update(bss_eval_two_source(truth, accompaniment, est_vocals, est_accomp))
    return metrics


# ----- entrypoint -------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mixture", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--estimates", required=True, nargs="+", type=Path)
    args = parser.parse_args()

    mixture, mix_sr = _load_mono(args.mixture)
    truth, truth_sr = _load_mono(args.truth)
    if mix_sr != truth_sr:
        raise RuntimeError(
            f"sample-rate mismatch: mixture {args.mixture} @ {mix_sr} Hz vs truth "
            f"{args.truth} @ {truth_sr} Hz (refusing to resample)"
        )
    mixture, truth = _align(mixture, truth)
    accompaniment = mixture - truth

    out: dict[str, dict[str, float | None]] = {
        # The do-nothing baseline: the mixture itself, graded as a vocal estimate.
        # Its accompaniment estimate is silence-free by construction (the mixture
        # again), which is exactly what total under-separation looks like.
        "_baseline_mixture": {
            "si_sdr": si_sdr_db(truth, mixture),
            # Null, not infinity: the mixture reconstructs itself perfectly, and
            # json.dump would emit a bare `Infinity` token that strict JSON
            # parsers reject.
            "si_sdr_to_mixture": None,
            "corr_truth": abs_correlation(truth, mixture),
            "corr_mixture": 1.0,
            "lsd_db": log_spectral_distance_db(truth, mixture),
        }
    }
    for estimate_path in args.estimates:
        est_vocals, est_sr = _load_mono(estimate_path)
        if est_sr != truth_sr:
            raise RuntimeError(
                f"sample-rate mismatch: truth @ {truth_sr} Hz vs estimate "
                f"{estimate_path} @ {est_sr} Hz (refusing to resample)"
            )
        est_accomp, inst_sr = _load_mono(_inst_path(estimate_path))
        if inst_sr != truth_sr:
            raise RuntimeError(
                f"sample-rate mismatch: truth @ {truth_sr} Hz vs instrumental of "
                f"{estimate_path} @ {inst_sr} Hz (refusing to resample)"
            )
        out[str(estimate_path)] = score_estimate(
            truth, accompaniment, mixture, est_vocals, est_accomp
        )

    json.dump(out, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
