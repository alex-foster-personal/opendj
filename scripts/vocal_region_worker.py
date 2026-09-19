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
"""Standalone demucs vocal-region worker (SPIKE-B2 candidate 1, ported).

Ported from the validated PoC ``.tmp/.tmp_spike_vocals/c1_demucs.py`` +
``common.py`` (SPIKE-B2, Tue 21 Jul 2026) with VERBATIM calibrated params:
htdemucs vocals stem -> RMS envelope (0.5 s hop) on vocals and mix ->
vocals/mix ratio -> hysteresis threshold (on 0.10, off 0.05) -> merge gaps
< 1.5 s, drop regions < 1.0 s. Confidence = clipped 2.5 x max ratio in the
region (same expression the PoC emitted).

Requirements (mini-PRD):
  ✔︎ ✅ emit regions JSON on stdout, logs on stderr only
    [if] run on a vocal track [then] stdout parses as JSON with regions
    [if] a log line lands on stdout [then ⛔️] JSON parse fails loudly
  ✔︎ ✅ region timestamps live in the SOURCE file's true timeline
    (NOTE-musicbot-alignment-learnings drift trap: a 48k analysis chain on
    a 44.1k file stretched timestamps 1.0884x). demucs resamples to the
    model rate (44100); resampling preserves wall-clock seconds, so we
    VERIFY that: the resampled duration must match ffprobe's source
    duration within tolerance, else raise - never emit drifted regions.
    [if] resampled duration drifts > max(0.5 s, 0.5%) from source [then ⛔️]
    [if] source is 48 kHz [then] region end_s still matches source seconds
  ✔︎ ✅ pure region maths importable WITHOUT torch installed
    (unit tests path-import this module; heavy imports live inside main())
    [if] `import vocal_region_worker` needs torch [then ⛔️]
  ✔︎ ✅ fail fast: missing file / ffmpeg / weird audio raises, no fallbacks
    [if] audio path does not exist [then ⛔️] non-zero exit + stderr reason
  ✔︎ ✅ WAV inputs decode via soundfile, never ffmpeg (Windows portability:
    ffmpeg is not guaranteed on PATH). Non-WAV inputs still need ffmpeg on
    PATH (optionally via MDT_FFMPEG) and fail fast, naming remedies, when
    it is absent - never a silent wrong decode.
    [if] audio_path.suffix is .wav [then] no ffmpeg subprocess is spawned
    [if] non-WAV input and ffmpeg is absent and MDT_FFMPEG unset [then ⛔️]
    RuntimeError names the file + MDT_FFMPEG + pre-transcode remedy

Run standalone:  uv run scripts/vocal_region_worker.py <audio-file>
Invoked by:      python -m apps.vocals trickle --live  (subprocess)

Known PoC gotchas kept (SPIKE-B2 section 2): torch pinned ==2.5.1 (>=2.6
flips torch.load weights_only and breaks demucs checkpoints); demucs 4.0.1
has no demucs.api; htdemucs cannot run on MPS (apply_model raises on
> 65536 output channels) so mps falls back to cpu with a warning.

-Claude
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

# ----- calibrated params (SPIKE-B2 section 6.6 - do NOT tune casually) --------
MODEL_NAME: str = "htdemucs"
HOP_S: float = 0.5  # RMS envelope hop
ON_RATIO: float = 0.10  # vocals-rms / mix-rms to enter a region
OFF_RATIO: float = 0.05  # ...to exit (hysteresis)
MERGE_GAP_S: float = 1.5  # merge regions separated by < this gap
MIN_REGION_S: float = 1.0  # drop merged regions shorter than this
CONFIDENCE_GAIN: float = 2.5  # confidence = min(1, GAIN * max ratio in region)
# When demucs leakage keeps every frame above OFF_RATIO, hysteresis never
# exits and paints the whole track (e.g. Nobody OLTF). Lift thresholds
# relative to that track's own baseline only in that stuck case - default
# SPIKE-B2 on/off stay for every track that can still exit.
ADAPT_BASE_PERCENTILE: float = 0.40
ADAPT_MARGIN: float = 0.08
# Drift guard (NOTE-musicbot-alignment-learnings): resampled duration must
# match the ffprobe source duration within this tolerance.
DRIFT_ABS_TOL_S: float = 0.5
DRIFT_REL_TOL: float = 0.005

SCHEMA: int = 2
SOURCE: str = "demucs-htdemucs"
_ACCELERATOR_FAILURE_MARKERS: dict[str, tuple[str, ...]] = {
    "cuda": ("cuda", "cudnn", "out of memory"),
    # htdemucs hits the documented MPS output-channel limit before torch's
    # device name appears in the exception, so retain that precise marker.
    "mps": ("mps", "metal", "output channels", "not implemented"),
}


# ----- pure region maths (dependency-light; unit-tested via path import) ------


def envelope_to_regions(
    env: list[tuple[float, float]],
    on: float,
    off: float,
    min_len: float = MIN_REGION_S,
    merge_gap: float = MERGE_GAP_S,
) -> list[tuple[float, float]]:
    """Hysteresis threshold (enter at >=on, exit at <off), merge close
    regions, drop short ones. Verbatim port of SPIKE-B2 common.py."""
    raw: list[tuple[float, float]] = []
    start: float | None = None
    for t, v in env:
        if start is None and v >= on:
            start = t
        elif start is not None and v < off:
            raw.append((start, t))
            start = None
    if start is not None:
        raw.append((start, env[-1][0]))
    merged: list[list[float]] = []
    for s, e in raw:
        if merged and s - merged[-1][1] < merge_gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(round(s, 2), round(e, 2)) for s, e in merged if e - s >= min_len]


def ratio_envelope(
    vocals_rms: list[float], mix_rms: list[float], hop_s: float
) -> list[tuple[float, float]]:
    """(t, vocals/mix) pairs; ratio 0 where the mix is silent (PoC rule)."""
    if len(vocals_rms) != len(mix_rms):
        raise ValueError(
            f"envelope length mismatch: vocals {len(vocals_rms)} != mix {len(mix_rms)}"
        )
    return [
        (i * hop_s, v / m if m > 1e-6 else 0.0)
        for i, (v, m) in enumerate(zip(vocals_rms, mix_rms, strict=False))
    ]


def region_confidence(
    ratio_env: list[tuple[float, float]],
    start_s: float,
    end_s: float,
    hop_s: float,
) -> float:
    """PoC confidence: clipped CONFIDENCE_GAIN x max ratio inside the region
    (at least the region's first hop is sampled, as in c1_demucs.py)."""
    i0 = int(start_s / hop_s)
    i1 = max(i0 + 1, int(end_s / hop_s))
    values = [v for t, v in ratio_env[i0:i1]]
    if not values:
        raise ValueError(f"region [{start_s}, {end_s}] has no envelope frames")
    return round(min(1.0, CONFIDENCE_GAIN * max(values)), 2)


def thresholds_for_ratio(ratio_values: list[float]) -> tuple[float, float, bool]:
    """SPIKE-B2 (on, off), or adapted when the envelope can never exit.

    Returns ``(on, off, adapted)``. Adaptation triggers only when every
    frame stays at/above OFF_RATIO (hysteresis stuck on). Otherwise the
    calibrated 0.10 / 0.05 pair is unchanged.
    """
    if not ratio_values:
        return ON_RATIO, OFF_RATIO, False
    if min(ratio_values) < OFF_RATIO:
        return ON_RATIO, OFF_RATIO, False
    ordered = sorted(ratio_values)
    # nearest-rank percentile, no numpy (worker pure-maths stays stdlib)
    idx = int(ADAPT_BASE_PERCENTILE * (len(ordered) - 1))
    base = ordered[idx]
    on = max(ON_RATIO, base + ADAPT_MARGIN)
    off = max(OFF_RATIO, on * 0.5)
    return on, off, True


def regions_payload(
    ratio_env: list[tuple[float, float]], duration_s: float, hop_s: float = HOP_S
) -> dict[str, Any]:
    """regions + coverage from a ratio envelope - the whole pure pipeline."""
    if duration_s <= 0:
        raise ValueError(f"non-positive duration_s: {duration_s}")
    values = [v for _t, v in ratio_env]
    on, off, adapted = thresholds_for_ratio(values)
    spans = envelope_to_regions(ratio_env, on=on, off=off)
    regions = [
        {
            "start_s": s,
            "end_s": e,
            "confidence": region_confidence(ratio_env, s, e, hop_s),
        }
        for s, e in spans
    ]
    coverage = 100.0 * sum(e - s for s, e in spans) / duration_s
    return {
        "regions": regions,
        "coverage_pct": round(coverage, 1),
        "fps": round(1.0 / hop_s, 4),
        "on_ratio": on,
        "off_ratio": off,
        "thresholds_adapted": adapted,
    }


# ----- heavy pipeline (torch/demucs imports stay INSIDE these functions) ------


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _is_accelerator_failure(device: str, exc: BaseException) -> bool:
    """Whether this device-layer error is eligible for a CPU retry.

    Audio decoding, model, and programming errors retain their original
    failure. Retrying every exception on CPU would hide those faults behind a
    plausible-looking but misleading cache entry.
    """
    return any(
        marker in str(exc).lower()
        for marker in _ACCELERATOR_FAILURE_MARKERS.get(device, ())
    )


def _rms_envelope(wav: Any, sr: int, hop_s: float) -> list[float]:
    mono = wav.mean(dim=0)
    hop = int(sr * hop_s)
    n = len(mono) // hop
    return [float(mono[i * hop : (i + 1) * hop].pow(2).mean().sqrt()) for i in range(n)]


def _separate_vocals(model: Any, wav: Any, device: str) -> Any:
    from demucs.apply import apply_model

    ref = wav.mean(0)
    normed = (wav - ref.mean()) / ref.std()
    sources = apply_model(model, normed[None], device=device, progress=False)[0]
    vocals = sources[model.sources.index("vocals")]
    return vocals * ref.std() + ref.mean()


def _read_wav_fastpath(audio_path: Path, model: Any) -> tuple[int, float, Any]:
    """WAV fast-path (Windows portability: bypasses demucs.audio.AudioFile
    and ffmpeg entirely). soundfile reads the native tensor, torchaudio
    resamples to the model's rate; source_sr/source_duration_s come from
    soundfile's own header info, mirroring what ffprobe gave the non-WAV
    path."""
    import soundfile as sf
    import torch
    import torchaudio
    from demucs.audio import convert_audio_channels

    info = sf.info(str(audio_path))
    source_sr = int(info.samplerate)
    source_duration_s = float(info.frames) / source_sr

    data, read_sr = sf.read(str(audio_path), dtype="float32", always_2d=True)
    if int(read_sr) != source_sr:
        raise RuntimeError(
            f"soundfile samplerate mismatch reading {audio_path}: "
            f"info reports {source_sr} Hz but read() returned {read_sr} Hz"
        )
    wav = torch.from_numpy(data.T).contiguous()  # (channels, samples) @ source_sr
    wav = convert_audio_channels(wav, model.audio_channels)
    wav = torchaudio.functional.resample(wav, source_sr, model.samplerate)
    return source_sr, source_duration_s, wav


def _read_via_ffmpeg(audio_path: Path, model: Any) -> tuple[int, float, Any]:
    """Non-WAV decode via demucs.audio.AudioFile (shells to ffmpeg/ffprobe).

    A decoder must be reachable first: MDT_FFMPEG (path to the ffmpeg
    executable, e.g. a D:/tools/ffmpeg drop's bin/ffmpeg.exe) is prepended
    onto PATH when set. Still missing -> fail fast naming the file and the
    remedies; never fall back to a wrong decode."""
    from demucs.audio import AudioFile

    ffmpeg_override = os.environ.get("MDT_FFMPEG")
    if ffmpeg_override:
        os.environ["PATH"] = os.pathsep.join(
            [str(Path(ffmpeg_override).parent), os.environ.get("PATH", "")]
        )
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            f"ffmpeg not found on PATH; cannot decode non-WAV input "
            f"{audio_path}. Fix one of: (1) set MDT_FFMPEG to the ffmpeg "
            f"executable path (e.g. a D:/tools/ffmpeg drop's "
            f"bin/ffmpeg.exe); (2) pre-transcode this file to WAV on the "
            f"Mac and re-run the worker there (WAV inputs bypass ffmpeg "
            f"entirely via the soundfile fast-path)."
        )

    af = AudioFile(audio_path)
    source_sr = int(af.samplerate())
    source_duration_s = float(af.duration)
    wav = af.read(streams=0, samplerate=model.samplerate, channels=model.audio_channels)
    return source_sr, source_duration_s, wav


def analyse(audio_path: Path, device_pref: str) -> dict[str, Any]:
    import torch
    from demucs.pretrained import get_model

    if not audio_path.is_file():
        raise FileNotFoundError(f"audio file missing: {audio_path}")

    device = device_pref
    if device == "auto":
        if torch.cuda.is_available():
            device = "cuda"
        elif torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"

    t0 = time.perf_counter()
    model = get_model(MODEL_NAME)
    model.eval()
    load_s = time.perf_counter() - t0
    _log(f"model={MODEL_NAME} device={device} load={load_s:.1f}s")

    if audio_path.suffix.lower() == ".wav":
        source_sr, source_duration_s, wav = _read_wav_fastpath(audio_path, model)
    else:
        source_sr, source_duration_s, wav = _read_via_ffmpeg(audio_path, model)

    # Drift trap guard (NOTE-musicbot-alignment-learnings): region seconds
    # are computed on the resampled 44.1k tensor; that is only the source
    # timeline if the resample preserved duration. Verify, never assume.
    resampled_duration_s = wav.shape[-1] / model.samplerate
    tolerance = max(DRIFT_ABS_TOL_S, DRIFT_REL_TOL * source_duration_s)
    if abs(resampled_duration_s - source_duration_s) > tolerance:
        raise RuntimeError(
            f"sample-rate drift detected for {audio_path}: source "
            f"{source_duration_s:.2f}s @ {source_sr} Hz but resampled "
            f"tensor is {resampled_duration_s:.2f}s @ {model.samplerate} Hz "
            f"(tolerance {tolerance:.2f}s). Refusing to emit drifted regions."
        )

    t0 = time.perf_counter()
    try:
        vocals = _separate_vocals(model, wav, device)
    except Exception as exc:
        if device != "cpu" and _is_accelerator_failure(device, exc):
            _log(f"[WARN] apply_model on {device} failed ({exc}); retrying cpu")
            device = "cpu"
            vocals = _separate_vocals(model, wav, device)
        else:
            raise
    separate_s = time.perf_counter() - t0

    sr = model.samplerate
    v_env = _rms_envelope(vocals, sr, HOP_S)
    m_env = _rms_envelope(wav, sr, HOP_S)
    ratio = ratio_envelope(v_env, m_env, HOP_S)
    payload = regions_payload(ratio, duration_s=source_duration_s)
    on_ratio = float(payload.pop("on_ratio"))
    off_ratio = float(payload.pop("off_ratio"))
    thresholds_adapted = bool(payload.pop("thresholds_adapted"))

    return {
        "schema": SCHEMA,
        "source": SOURCE,
        "audio_path": str(audio_path),
        "source_sample_rate": source_sr,
        "analysis_sample_rate": int(model.samplerate),
        "duration_s": round(source_duration_s, 3),
        "device": device,
        "params": {
            "model": MODEL_NAME,
            "hop_s": HOP_S,
            "on_ratio": on_ratio,
            "off_ratio": off_ratio,
            "thresholds_adapted": thresholds_adapted,
            "merge_gap_s": MERGE_GAP_S,
            "min_region_s": MIN_REGION_S,
            "confidence_gain": CONFIDENCE_GAIN,
        },
        "timings": {
            "load_s": round(load_s, 1),
            "separate_s": round(separate_s, 1),
        },
        **payload,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="uv run scripts/vocal_region_worker.py",
        description="demucs htdemucs vocal-region detection -> JSON on stdout",
    )
    parser.add_argument("audio", type=Path, help="path to the audio file")
    parser.add_argument(
        "--device",
        default="auto",
        choices=("auto", "cpu", "mps", "cuda"),
        help="torch device preference (auto prefers cuda > mps > cpu; "
        "htdemucs falls back mps/cuda -> cpu on failure)",
    )
    args = parser.parse_args()

    result = analyse(args.audio, args.device)
    _log(
        f"done: {len(result['regions'])} regions "
        f"cov={result['coverage_pct']}% sep={result['timings']['separate_s']}s"
    )
    json.dump(result, sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
