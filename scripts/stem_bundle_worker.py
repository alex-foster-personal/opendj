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
"""Standalone htdemucs 4-stem bundle writer for the webui stems contract.

Writes ``data/state/stems/<stable_id>/{vocals,drums,bass,other}.wav`` plus
``manifest.json`` matching ``apps.webui.server.stem_artifacts`` schema v1.

Torch/demucs stay out of the repo venv (PEP 723 / ``uv run`` only).

Alignment: stems are written at the model rate (44100) with frame counts
that match the demucs-resampled source timeline. Browser ``decodeAudioData``
resamples mix + stems to the AudioContext rate; wall-clock duration must
agree within one sample at that rate. We fail loud if demucs duration drifts
from ffprobe/source duration (same guard as vocal_region_worker).

-Claude
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

MODEL_NAME = "htdemucs"
DEMUCS_VERSION = "4.0.1"
STEM_PARTS = ("vocals", "drums", "bass", "other")
DRIFT_ABS_TOL_S = 0.5
DRIFT_REL_TOL = 0.005
_ACCELERATOR_FAILURE_MARKERS: dict[str, tuple[str, ...]] = {
    "cuda": ("cuda", "cudnn", "out of memory"),
    "mps": ("mps", "metal", "output channels", "not implemented"),
}


# ----- tiny helpers ----------------------------------------------------------


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_accelerator_failure(device: str, exc: BaseException) -> bool:
    return any(
        marker in str(exc).lower()
        for marker in _ACCELERATOR_FAILURE_MARKERS.get(device, ())
    )


def _ffprobe_duration_s(audio_path: Path) -> float:
    import json as _json
    import subprocess

    ffmpeg = os.environ.get("MDT_FFMPEG", "ffmpeg")
    ffprobe = "ffprobe"
    if ffmpeg != "ffmpeg":
        cand = Path(ffmpeg).with_name("ffprobe")
        if cand.is_file():
            ffprobe = str(cand)
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(audio_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {audio_path}: {proc.stderr.strip() or proc.returncode}"
        )
    payload = _json.loads(proc.stdout)
    duration = float(payload["format"]["duration"])
    if duration <= 0:
        raise RuntimeError(f"non-positive duration from ffprobe: {duration}")
    return duration


def _guard_duration(source_s: float, model_s: float) -> None:
    tol = max(DRIFT_ABS_TOL_S, DRIFT_REL_TOL * source_s)
    if abs(model_s - source_s) > tol:
        raise RuntimeError(
            f"demucs timeline drift: source={source_s:.3f}s model={model_s:.3f}s "
            f"tol={tol:.3f}s"
        )


# ----- load / separate -------------------------------------------------------


def _load_audio(audio_path: Path, model: Any) -> tuple[Any, int, float]:
    """Return (wav[C,T] float tensor at model rate, source_sr, source_duration_s)."""
    import torch
    from demucs.audio import AudioFile, convert_audio

    suffix = audio_path.suffix.lower()
    if suffix == ".wav":
        import soundfile as sf

        info = sf.info(str(audio_path))
        source_sr = int(info.samplerate)
        source_duration_s = float(info.frames) / source_sr
        data, read_sr = sf.read(str(audio_path), dtype="float32", always_2d=True)
        if int(read_sr) != source_sr:
            raise RuntimeError(
                f"soundfile sr mismatch: header {source_sr} read {read_sr}"
            )
        wav = torch.from_numpy(data.T)  # [C, T]
        wav = convert_audio(wav, source_sr, model.samplerate, model.audio_channels)
        return wav, source_sr, source_duration_s

    if shutil.which(
        os.environ.get("MDT_FFMPEG", "ffmpeg")
    ) is None and not os.environ.get("MDT_FFMPEG"):
        raise RuntimeError(
            f"non-WAV input {audio_path.name} needs ffmpeg on PATH "
            "(or MDT_FFMPEG) / pre-transcode to wav"
        )
    source_duration_s = _ffprobe_duration_s(audio_path)
    # AudioFile shells to ffmpeg; returns [C, T] at native rate then we convert.
    wav = AudioFile(str(audio_path)).read(
        streams=0,
        samplerate=model.samplerate,
        channels=model.audio_channels,
    )
    # demucs AudioFile already returns model rate when samplerate= is set.
    import torch as _torch

    if not isinstance(wav, _torch.Tensor):
        wav = _torch.as_tensor(wav)
    return wav, model.samplerate, source_duration_s


def _separate_all(model: Any, wav: Any, device: str) -> dict[str, Any]:
    from demucs.apply import apply_model

    ref = wav.mean(0)
    normed = (wav - ref.mean()) / ref.std()
    sources = apply_model(model, normed[None], device=device, progress=True)[0]
    out: dict[str, Any] = {}
    for name in STEM_PARTS:
        idx = model.sources.index(name)
        out[name] = sources[idx] * ref.std() + ref.mean()
    return out


def _pick_device(requested: str) -> str:
    import torch

    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    # htdemucs cannot run on MPS (output-channel limit); skip to cpu.
    return "cpu"


def _write_wav(path: Path, tensor: Any, sample_rate: int) -> None:
    import numpy as np
    import soundfile as sf

    # tensor [C, T] -> [T, C]
    arr = tensor.detach().cpu().numpy().T.astype(np.float32, copy=False)
    if arr.ndim != 2 or arr.shape[1] not in (1, 2):
        raise RuntimeError(f"unexpected stem shape {arr.shape} for {path.name}")
    sf.write(str(path), arr, sample_rate, subtype="PCM_16")


def write_bundle(
    *,
    audio_path: Path,
    stable_id: str,
    out_dir: Path,
    device: str = "auto",
) -> dict[str, Any]:
    """Separate one track and atomically write a v1 stem bundle into out_dir."""
    from demucs.pretrained import get_model

    if not audio_path.is_file():
        raise FileNotFoundError(f"audio missing: {audio_path}")
    audio_path = audio_path.resolve()
    device_used = _pick_device(device)
    _log(f"[stem] load model={MODEL_NAME} device={device_used} id={stable_id}")
    t0 = time.time()
    model = get_model(MODEL_NAME)
    model.eval()
    wav, _source_sr, source_duration_s = _load_audio(audio_path, model)
    model_duration_s = float(wav.shape[-1]) / float(model.samplerate)
    _guard_duration(source_duration_s, model_duration_s)

    try:
        parts = _separate_all(model, wav.to(device_used), device_used)
    except Exception as exc:
        if device_used != "cpu" and _is_accelerator_failure(device_used, exc):
            _log(f"[WARN] apply_model on {device_used} failed ({exc}); retrying cpu")
            device_used = "cpu"
            parts = _separate_all(model, wav.cpu(), device_used)
        else:
            raise

    frame_count = int(parts["vocals"].shape[-1])
    for name, tensor in parts.items():
        if int(tensor.shape[-1]) != frame_count:
            raise RuntimeError(
                f"stem frame mismatch: {name}={tensor.shape[-1]} vocals={frame_count}"
            )
        if int(tensor.shape[0]) != int(model.audio_channels):
            raise RuntimeError(
                f"stem channel mismatch: {name}={tensor.shape[0]} "
                f"expected={model.audio_channels}"
            )

    out_dir = out_dir.resolve()
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".stem-{stable_id}-", dir=str(out_dir.parent)))
    try:
        for name, tensor in parts.items():
            _write_wav(tmp / f"{name}.wav", tensor, int(model.samplerate))
        manifest = {
            "schema_version": 1,
            "stable_id": stable_id,
            "model": {"name": MODEL_NAME, "version": DEMUCS_VERSION},
            "source": {
                "path": str(audio_path),
                "sha256": _sha256_file(audio_path),
            },
            "files": {name: f"{name}.wav" for name in STEM_PARTS},
        }
        (tmp / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        if out_dir.exists():
            shutil.rmtree(out_dir)
        tmp.rename(out_dir)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    wall_s = time.time() - t0
    result = {
        "stable_id": stable_id,
        "out_dir": str(out_dir),
        "sample_rate_hz": int(model.samplerate),
        "frame_count": frame_count,
        "channel_count": int(model.audio_channels),
        "source_duration_s": round(source_duration_s, 3),
        "device_used": device_used,
        "wall_s": round(wall_s, 1),
        "realtime_factor": round(wall_s / source_duration_s, 3),
    }
    _log(
        f"[stem] done id={stable_id} wall={wall_s:.1f}s "
        f"rt={result['realtime_factor']}x device={device_used}"
    )
    return result


# ----- CLI -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Write a v1 htdemucs stem bundle")
    p.add_argument("--audio", type=Path, required=True)
    p.add_argument("--stable-id", required=True)
    p.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="bundle directory (e.g. data/state/stems/<stable_id>)",
    )
    p.add_argument(
        "--device",
        default=os.environ.get("MDT_STEM_WORKER_DEVICE")
        or os.environ.get("MDT_VOCAL_WORKER_DEVICE")
        or "auto",
        choices=("auto", "cpu", "cuda", "mps"),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = write_bundle(
        audio_path=args.audio,
        stable_id=args.stable_id,
        out_dir=args.out_dir,
        device=args.device,
    )
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
