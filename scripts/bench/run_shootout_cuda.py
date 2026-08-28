# /// script
# # Pinned to CPython 3.10 on purpose: diffq 0.2.4, which dequantizes mdx_q and
# # mdx_extra_q, ships a prebuilt win_amd64 wheel only up to cp310. On 3.11+ the
# # build falls back to compiling its C extension, which needs MSVC Build Tools
# # the Windows GPU box does not have. Widening this drops the quantized models.
# requires-python = ">=3.10,<3.11"
# dependencies = [
#   "torch==2.5.1",
#   "torchaudio==2.5.1",
#   "demucs==4.0.1",
#   "diffq>=0.2.4",
#   "soundfile>=0.12",
#   "numpy<2",
#   "scipy>=1.10",
#   "fast-bss-eval>=0.1.4",
#   "packaging>=23",
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
"""Run the multi-track model shootout on any CUDA box, data-local.

Runs wherever an NVIDIA GPU and the declared inputs are available. Keeping
compute close to the inputs avoids transferring the full audio corpus.
Everything runs in one process on one machine; only the result JSON needs
to be transferred afterwards.

Self-contained by PEP 723, which is also the repo rule for anything touching
torch or demucs: heavy ML dependencies never enter the repo venv.

Sibling modules (shootout_spec, pick_vocal_window, separation_metrics) are
imported normally, which works because Python puts the running script's own
directory on sys.path. Copy all four files into one directory to run remotely.

Method per track:
  1. the excerpt window is chosen from the TRUE vocal stem by energy, never
     from a separator's guess;
  2. the IDENTICAL sample range is sliced from the mixture and from the truth
     with soundfile, so the pair is sample-exact (no ffmpeg seek rounding);
  3. every model separates that excerpt at identical knobs on the GPU;
  4. SI-SDR, BSS Eval v4 SDR/SIR/SAR, mixture correlation and log-spectral
     distance are computed against the truth.

Requirements (mini-PRD):
  ✔︎ ✅ refuses to run on CPU. A silent CPU fallback would take hours and the
    separate_s column would become meaningless next to the Modal numbers.
    [if] torch.cuda.is_available() is False [then ⛔️] RuntimeError
  ✔︎ ✅ mixture and vocals must be the same length before any slicing.
    [if] a stem is truncated relative to its mixture [then ⛔️] RuntimeError
    [if] the sliced excerpt is short of the requested length [then ⛔️] RuntimeError
  ✔︎ ✅ a model that fails to load is recorded with its error and the run
    continues; a model that fails on EVERY track is reported as failed.
    [if] every model fails on a track [then ⛔️] RuntimeError
  ✔︎ ✅ results are written to the JSON after EVERY track, not at the end, so a
    crash on track 9 cannot destroy tracks 1 to 8.
    [if] the process dies mid-run [then] the JSON holds every completed track

Run:
  uv run run_shootout_cuda.py --musdb-dir D:/asset-store/datasets/musdb-extract \
      --out-dir D:/asset-store/shootout --json-out D:/asset-store/shootout/model_shootout.json

-Claude
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pick_vocal_window
import separation_metrics
import soundfile as sf
import torch
from demucs.apply import apply_model
from demucs.pretrained import get_model
from shootout_spec import EXCERPT_LEN_S, MODELS, OVERLAP, SHIFTS, TRACKS, Track

# MUSDB18-HQ ships mixture and stems sample-identical. Anything past a single
# hop is a truncated file, and scoring against a truncated truth is silently
# meaningless, which is the exact failure this benchmark exists to avoid.
_DURATION_TOL_S: float = 0.05


# ----- audio io ---------------------------------------------------------------


def _read_stereo(path: Path) -> tuple[np.ndarray, int]:
    if not path.is_file():
        raise RuntimeError(f"audio file does not exist: {path}")
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return data, int(sr)


def _resolve_pair(musdb_dir: Path, track: Track) -> tuple[Path, Path]:
    mixture = musdb_dir / f"{track.title}_mixture.wav"
    vocals = musdb_dir / f"{track.title}_vocals.wav"
    for path in (mixture, vocals):
        if not path.is_file():
            raise RuntimeError(f"MUSDB18-HQ input missing: {path}")
    return mixture, vocals


def _slice_excerpt(
    data: np.ndarray, sr: int, start_s: float, length_s: float, label: str
) -> np.ndarray:
    start = round(start_s * sr)
    stop = start + round(length_s * sr)
    if stop > len(data):
        raise RuntimeError(
            f"{label}: window [{start_s:.2f}s, {start_s + length_s:.2f}s] runs past "
            f"the {len(data) / sr:.2f}s source"
        )
    excerpt = data[start:stop]
    got_s = len(excerpt) / sr
    if abs(got_s - length_s) > _DURATION_TOL_S:
        raise RuntimeError(f"{label}: sliced {got_s:.3f}s, expected {length_s:.3f}s")
    return excerpt


def _write_flac(path: Path, data: np.ndarray, sr: int) -> None:
    """16-bit FLAC. MUSDB18-HQ is a 16-bit source, so 24-bit would carry no extra
    information. Peak-normalise only when hot; every metric here is scale
    invariant, so this cannot move a score."""
    peak = float(np.abs(data).max())
    if peak > 0.99:
        data = data * (0.99 / peak)
    sf.write(str(path), data, sr, format="FLAC", subtype="PCM_16")


# ----- separation -------------------------------------------------------------


def _separate(
    model_name: str, mixture: np.ndarray, sr: int, device: str
) -> tuple[np.ndarray, np.ndarray, float, float]:
    """One model over one excerpt. Returns (vocals, instrumental, separate_s, load_s).

    The instrumental is the mixture minus the vocal estimate, which is exactly
    what `demucs --two-stems=vocals` calls no_vocals, and it is required for the
    SIR/SAR under-separation check.
    """
    load_t0 = time.perf_counter()
    bag = get_model(model_name)
    bag.eval()
    load_s = time.perf_counter() - load_t0

    if sr != bag.samplerate:
        raise RuntimeError(
            f"{model_name} expects {bag.samplerate} Hz but the excerpt is {sr} Hz "
            "(refusing to resample silently)"
        )
    wav = torch.from_numpy(mixture.T.copy())  # (channels, samples)
    if wav.shape[0] != bag.audio_channels:
        raise RuntimeError(
            f"{model_name} expects {bag.audio_channels} channels, excerpt has "
            f"{wav.shape[0]}"
        )
    ref = wav.mean(0)
    normed = (wav - ref.mean()) / ref.std()

    torch.cuda.synchronize()
    t0 = time.perf_counter()
    sources = apply_model(
        bag, normed[None], device=device, overlap=OVERLAP, shifts=SHIFTS, progress=False
    )[0]
    torch.cuda.synchronize()
    separate_s = time.perf_counter() - t0

    vocals = sources[bag.sources.index("vocals")] * ref.std() + ref.mean()
    instrumental = wav - vocals
    del bag, sources
    torch.cuda.empty_cache()
    return (
        vocals.T.cpu().numpy(),
        instrumental.T.cpu().numpy(),
        separate_s,
        load_s,
    )


# ----- per-track pipeline -----------------------------------------------------


def _run_track(track: Track, musdb_dir: Path, out_dir: Path, device: str) -> dict[str, Any]:
    mixture_path, vocals_path = _resolve_pair(musdb_dir, track)
    mixture_full, mix_sr = _read_stereo(mixture_path)
    vocals_full, voc_sr = _read_stereo(vocals_path)
    if mix_sr != voc_sr:
        raise RuntimeError(
            f"{track.title}: mixture @ {mix_sr} Hz vs vocals @ {voc_sr} Hz"
        )
    if abs(len(mixture_full) - len(vocals_full)) > _DURATION_TOL_S * mix_sr:
        raise RuntimeError(
            f"{track.title}: mixture {len(mixture_full) / mix_sr:.3f}s != vocals "
            f"{len(vocals_full) / voc_sr:.3f}s -- one is truncated"
        )

    energy, _sr = pick_vocal_window._hop_energy(vocals_path, pick_vocal_window.HOP_S)
    window = pick_vocal_window.pick_window(
        energy, pick_vocal_window.HOP_S, EXCERPT_LEN_S, step_s=1.0
    )
    start_s, length_s = window["start_s"], window["length_s"]
    print(
        f"\n=== {track.title}\n    window [{start_s:.1f}s -> {window['end_s']:.1f}s] "
        f"on TRUE vocal energy, rms={window['rms_dbfs']} dBFS, silent hops "
        f"{100 * window['silent_frac']:.1f}%",
        flush=True,
    )

    mixture = _slice_excerpt(mixture_full, mix_sr, start_s, length_s, f"{track.title} mixture")
    truth = _slice_excerpt(vocals_full, voc_sr, start_s, length_s, f"{track.title} truth")

    out_dir.mkdir(parents=True, exist_ok=True)
    mix_path = out_dir / f"{track.slug}-mixture.flac"
    truth_path = out_dir / f"{track.slug}-truth-vocals.flac"
    _write_flac(mix_path, mixture, mix_sr)
    _write_flac(truth_path, truth, voc_sr)

    timings: dict[str, dict[str, float]] = {}
    errors: dict[str, str] = {}
    estimates: list[Path] = []
    for model_name in MODELS:
        try:
            vocals, instrumental, separate_s, load_s = _separate(
                model_name, mixture, mix_sr, device
            )
        except Exception as exc:  # recorded and reported, never swallowed
            errors[model_name] = f"{type(exc).__name__}: {exc}"
            print(f"    [FAIL] {model_name}: {exc}", flush=True)
            continue
        vocal_path = out_dir / f"{track.slug}-{model_name}.flac"
        _write_flac(vocal_path, vocals, mix_sr)
        _write_flac(out_dir / f"{track.slug}-{model_name}.inst.flac", instrumental, mix_sr)
        timings[model_name] = {
            "separate_s": round(separate_s, 2),
            "load_s": round(load_s, 2),
        }
        estimates.append(vocal_path)
        print(f"    [OK] {model_name} separate_s={separate_s:.2f}", flush=True)

    if not estimates:
        raise RuntimeError(f"{track.title}: every model failed, nothing to score")

    scored = _score(mix_path, truth_path, estimates, track.slug)
    results: dict[str, Any] = {}
    for model_name in MODELS:
        if model_name in errors:
            results[model_name] = {"error": errors[model_name]}
            continue
        results[model_name] = {**timings[model_name], **scored["by_model"][model_name]}
    return {
        "track": track.title,
        "slug": track.slug,
        "genre": track.genre,
        "window": window,
        "baseline_mixture": scored["baseline"],
        "results": results,
    }


def _score(
    mix_path: Path, truth_path: Path, estimates: list[Path], slug: str
) -> dict[str, Any]:
    """Reuse separation_metrics wholesale so local and remote runs agree exactly."""
    mixture, mix_sr = separation_metrics._load_mono(mix_path)
    truth, truth_sr = separation_metrics._load_mono(truth_path)
    if mix_sr != truth_sr:
        raise RuntimeError(f"mixture @ {mix_sr} Hz vs truth @ {truth_sr} Hz")
    mixture, truth = separation_metrics._align(mixture, truth)
    accompaniment = mixture - truth

    by_model: dict[str, dict[str, float]] = {}
    for estimate_path in estimates:
        est_vocals, est_sr = separation_metrics._load_mono(estimate_path)
        if est_sr != truth_sr:
            raise RuntimeError(f"{estimate_path} @ {est_sr} Hz vs truth @ {truth_sr} Hz")
        est_accomp, _ = separation_metrics._load_mono(
            separation_metrics._inst_path(estimate_path)
        )
        model_name = estimate_path.stem.replace(f"{slug}-", "")
        by_model[model_name] = separation_metrics.score_estimate(
            truth, accompaniment, mixture, est_vocals, est_accomp
        )
    baseline = {
        "si_sdr": separation_metrics.si_sdr_db(truth, mixture),
        "corr_truth": separation_metrics.abs_correlation(truth, mixture),
        "corr_mixture": 1.0,
        "lsd_db": separation_metrics.log_spectral_distance_db(truth, mixture),
    }
    return {"by_model": by_model, "baseline": baseline}


# ----- entrypoint -------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--musdb-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--json-out", required=True, type=Path)
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="run only the first N tracks. For validating the pipeline before "
        "committing an hour of GPU time; 0 means the full fixed track set.",
    )
    args = parser.parse_args()
    tracks = TRACKS[: args.limit] if args.limit else TRACKS

    if not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA is unavailable. This benchmark reports separate_s as a cost "
            "measurement, so a silent CPU fallback would publish a wrong number "
            "as well as taking hours."
        )
    device = "cuda"
    print(f"[OK] {torch.cuda.get_device_name(0)}, torch {torch.__version__}", flush=True)

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "dataset": "MUSDB18-HQ test",
        "reference_kind": "true-stem",
        "excerpt_len_s": EXCERPT_LEN_S,
        "config": {
            "overlap": OVERLAP,
            "shifts": SHIFTS,
            "gpu": torch.cuda.get_device_name(0),
        },
        "models": list(MODELS),
        "tracks": [],
    }

    wall0 = time.perf_counter()
    for index, track in enumerate(tracks, start=1):
        block = _run_track(track, args.musdb_dir, args.out_dir, device)
        manifest["tracks"].append(block)
        # Persist after every track: a crash on the last one must not destroy
        # an hour of GPU work that already produced valid numbers.
        args.json_out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        scored = {n: r["si_sdr"] for n, r in block["results"].items() if "si_sdr" in r}
        best = max(scored, key=lambda k: scored[k])
        print(
            f"    -> {index}/{len(tracks)} best={best} {scored[best]:.2f} dB "
            f"(elapsed {time.perf_counter() - wall0:.0f}s)",
            flush=True,
        )

    print(f"\n[OK] wrote {args.json_out} in {time.perf_counter() - wall0:.0f}s")


if __name__ == "__main__":
    main()
