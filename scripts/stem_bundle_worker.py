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

Writes ``data/state/stems/<stable_id>/{vocals,drums,bass,other}.<ext>`` plus
a schema v3 ``manifest.json`` (apps.stems.artifacts), where
``<ext>`` is decided by apps/stems/stem_size_policy.py's source-extension
codec policy, not hardcoded WAV.

Torch/demucs stay out of the repo venv (PEP 723 / ``uv run`` only).

Alignment: stems are written at the model rate (44100) with frame counts
that match the demucs-resampled source timeline. Browser ``decodeAudioData``
resamples mix + stems to the AudioContext rate; wall-clock duration must
agree within one sample at that rate. We fail loud if demucs duration drifts
from ffprobe/source duration (same guard as vocal_region_worker).

OUTPUT CODEC POLICY (apps/stems/stem_size_policy.py is the source of truth,
imported directly, not mirrored: unlike modal_roformer_spike.py and
modal_vocal_farm.py -- which run inside a remote Modal GPU container with
only the single script file baked into the image, no ``apps`` package on
disk at all -- this script always runs as a local subprocess from a full
repo checkout (see apps/webui/server/routes/stem_tiers.py's ``_repo_root()``
and apps/stems/cli.py's ``WORKER_SCRIPT``), so ``apps.stems.stem_size_policy``
is reachable on disk once ``main()`` puts the repo root on ``sys.path``. A
mirror would only add drift risk for zero benefit here):
  * source is lossless (flac/wav/aif/aiff) -> FLAC, always PCM_16 (house
    convention, not a source-bit-depth match -- see modal_vocal_farm.py's
    ``_encode_flac`` and modal_demucs_ab.py's OUTPUT POLICY comment)
  * source is lossy (mp3/m4a) -> MP3, bitrate <= source: LAME 320 CBR when
    the source itself is >= 320 kbps, else LAME V0 VBR, with a rung-2
    re-encode (nearest standard CBR <= floor(source_kbps)) for any part
    whose rung-1 bytes exceed the source file
  * an unrecognised source extension fails fast, before any GPU spend
  * size-vs-source is a KPI/target, not a hard gate: an over-budget part is
    kept and recorded in manifest.json's ``size_policy``, never discarded

MANIFEST SCHEMA: THIS SCRIPT WRITES SCHEMA V3, NOT V1. v1's reader
(``_load_v1_bundle`` in apps/stems/artifacts.py) hardcodes
``media_type="audio/wav"`` and parses every declared file as a WAV RIFF
header (``read_wav_metadata``) regardless of its actual extension --
writing non-WAV files under a v1 manifest reproduces the exact
"v1 manifests declare WAV while files are FLAC" bug documented in
docs/architecture.md's on-disk corpora table. Schema v3 declares an
explicit ``layout`` plus an ``audio`` block (sample_rate/frame_count/
channels measured by this script, not read back out of the files) and
accepts any single codec shared across every part (``_resolve_v3_files``)
-- built for exactly this reason.

MINI-PRD
--------
Status key: `->` out of scope | `?` todo | `OK` done | `OK+run` done + ran +
works as expected.

  OK output codec follows apps/stems/stem_size_policy.py's source-
    extension policy instead of always writing WAV. [issue #1497 / STEM-02]
    Verified via the pure _stem_file_suffix path and code inspection only --
    the full write_bundle GPU/mp3-ladder/manifest path has not been run
    end-to-end on this box (no torch/demucs/ffmpeg-verified environment).
    [if] the source is .mp3 [then] every stem part is written as .mp3, and
      manifest.json declares schema v3 with a matching audio block
    [if] the source is .flac/.wav/.aif/.aiff [then] every part is FLAC
    [if] the source extension is unrecognised [then not-ok] raise before
      model load, no GPU spend
  OK+run guard test with a sabotage-verify pass:
    tests/scripts/test_stem_bundle_worker_codec.py
  OK+run no ffmpeg (the installed app): non-WAV decodes via ``odj-audio
    decode`` into a temp WAV, neither -> raise before model load, and mp3
    policy parts are FLAC with size_policy.codec_fallback (STEM-50, STEM-51).
    tests/scripts/test_stem_bundle_worker_decode.py

-Claude
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
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


def _stem_file_suffix(audio_path: Path) -> str:
    """The output file extension (with leading dot) for one source file,
    decided by apps/stems/stem_size_policy.py's source-extension codec
    policy. Pure and GPU-free so it can be checked before any model load,
    and so a guard test can exercise it directly
    (tests/scripts/test_stem_bundle_worker_codec.py)."""
    from apps.stems import stem_size_policy as pol

    return "." + pol.stem_output_codec_for_source_ext(audio_path.suffix)


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


def _ffmpeg_reachable() -> bool:
    from apps.shared.odj_audio_decode import ffmpeg_on_path

    return ffmpeg_on_path()


def _read_wav(wav_path: Path, model: Any) -> tuple[Any, int, float]:
    """(wav[C,T] at model rate, source_sr, duration_s) of a WAV, via soundfile."""
    import soundfile as sf
    import torch
    from demucs.audio import convert_audio

    info = sf.info(str(wav_path))
    source_sr = int(info.samplerate)
    source_duration_s = float(info.frames) / source_sr
    data, read_sr = sf.read(str(wav_path), dtype="float32", always_2d=True)
    if int(read_sr) != source_sr:
        raise RuntimeError(f"soundfile sr mismatch: header {source_sr} read {read_sr}")
    wav = convert_audio(torch.from_numpy(data.T), source_sr, model.samplerate, model.audio_channels)
    return wav, source_sr, source_duration_s


def _preflight_decoder(audio_path: Path) -> None:
    """Raise before the model loads when nothing here decodes the source."""
    if audio_path.suffix.lower() != ".wav" and not _ffmpeg_reachable():
        from apps.shared.odj_audio_decode import resolve_odj_audio

        resolve_odj_audio(audio_path)


def _load_audio(audio_path: Path, model: Any) -> tuple[Any, int, float]:
    """Return (wav[C,T] float tensor at model rate, source_sr, source_duration_s).

    WAV: soundfile. Anything else: ffmpeg when it resolves (a development
    machine, unchanged), else ``odj-audio decode`` (the installed app); with
    neither, the error names both (STEM-50).
    """
    suffix = audio_path.suffix.lower()
    if suffix == ".wav":
        return _read_wav(audio_path, model)

    if not _ffmpeg_reachable():
        from apps.shared.odj_audio_decode import decoded_wav

        with decoded_wav(audio_path, prefix="odj-stem-decode-") as decoded:
            _log(f"[stem] decoded {audio_path.name} via odj-audio ({decoded.sample_rate} Hz)")
            return _read_wav(decoded.path, model)
    from demucs.audio import AudioFile

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


def _effective_output_codec(policy_codec: str) -> tuple[str, str | None]:
    """(codec to write, why it is not the policy's, or None). MP3 parts need
    ffmpeg's libmp3lame, which the installed app lacks: there they are FLAC,
    recorded in the manifest, not a failure after the separation (STEM-51)."""
    if policy_codec == "mp3" and not _ffmpeg_reachable():
        return "flac", (
            "policy codec mp3 needs ffmpeg (libmp3lame), which this install "
            "does not have; wrote lossless flac instead"
        )
    return policy_codec, None


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


def _encode_flac_bytes(tensor: Any, sample_rate: int) -> bytes:
    """One stem as 16-bit FLAC bytes (house convention, always PCM_16 -- see
    modal_vocal_farm.py's ``_encode_flac`` and modal_demucs_ab.py's OUTPUT
    POLICY comment: control/eval FLAC output is always 16-bit, never a
    source-bit-depth match)."""
    import io

    import numpy as np
    import soundfile as sf

    arr = tensor.detach().cpu().numpy().T.astype(np.float32, copy=False)
    if arr.ndim != 2 or arr.shape[1] not in (1, 2):
        raise RuntimeError(f"unexpected stem shape {arr.shape}")
    buffer = io.BytesIO()
    sf.write(buffer, arr, sample_rate, format="FLAC", subtype="PCM_16")
    return buffer.getvalue()


def _ffmpeg_encode_mp3(flac_bytes: bytes, out_path: Path, *, cbr_kbps: int | None) -> bytes:
    """LAME-encode a FLAC intermediate to mp3: CBR at ``cbr_kbps`` if given,
    else V0 VBR.

    ``out_path`` MUST be a real (seekable) file, not a pipe -- a VBR mp3
    muxed to a pipe comes out with no Xing/VBR header (ffmpeg writes that
    header as a placeholder up front and seeks back to patch in the real
    frame count once encoding finishes), which makes every fast duration
    reader (ffprobe, mutagen, the webui player) read the wrong length even
    though the audio decodes fine. See modal_roformer_spike.py's
    ``_ffmpeg_encode_mp3_v0`` for the same finding, checked there directly
    against ffmpeg's own behaviour.
    """
    import subprocess

    args = ["-b:a", f"{cbr_kbps}k"] if cbr_kbps is not None else ["-qscale:a", "0"]
    proc = subprocess.run(
        [
            os.environ.get("MDT_FFMPEG", "ffmpeg"),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-codec:a",
            "libmp3lame",
            *args,
            str(out_path),
        ],
        input=flac_bytes,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not out_path.is_file():
        stderr_tail = proc.stderr.decode(errors="replace")[-500:]
        msg = f"ffmpeg mp3 encode failed rc={proc.returncode}: {stderr_tail}"
        raise RuntimeError(msg)
    return out_path.read_bytes()


def _encode_stem_part(
    tensor: Any,
    sample_rate: int,
    *,
    codec: str,
    mp3_settings: str | None,
    source_bytes: int,
    source_kbps: float,
    out_path: Path,
) -> tuple[bytes, str | None]:
    """Write one stem part at ``out_path`` in ``codec``; return (bytes,
    final mp3 rung-2 settings string or None if rung 1 held / codec is flac).

    Size-vs-source is a KPI/target, not a hard gate (see
    apps/stems/stem_size_policy.py's module docstring): a part that still
    exceeds source bytes after the rung-2 re-encode is written anyway.
    """
    from apps.stems import stem_size_policy as pol

    flac_bytes = _encode_flac_bytes(tensor, sample_rate)
    if codec == "flac":
        out_path.write_bytes(flac_bytes)
        return flac_bytes, None

    cbr_kbps = pol.MP3_CBR_KBPS if mp3_settings == "lame_320kbps_cbr" else None
    encoded = _ffmpeg_encode_mp3(flac_bytes, out_path, cbr_kbps=cbr_kbps)
    if not pol.lossy_stem_exceeds_source(
        source_bytes=source_bytes, part_bytes=len(encoded), codec=codec
    ):
        return encoded, None

    rung2_cbr = pol.mp3_rung2_cbr_kbps(source_kbps)
    encoded = _ffmpeg_encode_mp3(flac_bytes, out_path, cbr_kbps=rung2_cbr)
    return encoded, f"lame_{rung2_cbr}kbps_cbr"


def _manifest_dict(
    *,
    stable_id: str,
    audio_path: Path,
    source_sha256: str,
    ext: str,
    output_codec: str,
    audio: dict[str, int],
    size_policy: dict[str, Any],
) -> dict[str, Any]:
    """The schema v3 manifest body (apps.stems.artifacts). Pure
    and GPU-free so tests/scripts/test_stem_bundle_worker_codec.py can round-
    trip it through the real reader (load_stem_bundle) without a GPU.

    ``audio`` and ``size_policy`` are already-built manifest sub-objects
    (see write_bundle) rather than their individual fields, to keep this
    function's own argument count small."""
    return {
        "schema_version": 3,
        "stable_id": stable_id,
        "layout": "demucs4",
        "model": {"name": MODEL_NAME, "version": DEMUCS_VERSION},
        "source": {"path": str(audio_path), "sha256": source_sha256},
        "files": {name: f"{name}{ext}" for name in STEM_PARTS},
        "audio": audio,
        "codec": output_codec,
        "size_policy": size_policy,
    }


def write_bundle(
    *,
    audio_path: Path,
    stable_id: str,
    out_dir: Path,
    device: str = "auto",
) -> dict[str, Any]:
    """Separate one track and atomically write a v3 stem bundle into out_dir."""
    from demucs.pretrained import get_model

    from apps.stems import stem_size_policy as pol

    if not audio_path.is_file():
        raise FileNotFoundError(f"audio missing: {audio_path}")
    audio_path = audio_path.resolve()
    # Resolve the output codec before spending any GPU time: an unrecognised
    # source extension is a policy gap, not a guess (UnknownSourceFormatError).
    policy_codec = pol.stem_output_codec_for_source_ext(audio_path.suffix)
    output_codec, codec_fallback = _effective_output_codec(policy_codec)
    ext = "." + output_codec
    if codec_fallback is not None:
        _log(f"[stem] {codec_fallback}")
    _preflight_decoder(audio_path)
    source_bytes = audio_path.stat().st_size

    device_used = _pick_device(device)
    _log(f"[stem] load model={MODEL_NAME} device={device_used} id={stable_id}")
    t0 = time.time()
    model = get_model(MODEL_NAME)
    model.eval()
    wav, _source_sr, source_duration_s = _load_audio(audio_path, model)
    model_duration_s = float(wav.shape[-1]) / float(model.samplerate)
    _guard_duration(source_duration_s, model_duration_s)
    source_kbps = pol.effective_bitrate_kbps(source_bytes, source_duration_s)
    mp3_settings_rung1 = (
        pol.mp3_lame_settings_for_source_kbps(source_kbps) if output_codec == "mp3" else None
    )

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
        part_sizes: dict[str, int] = {}
        mp3_settings_by_part: dict[str, str] = {}
        for name, tensor in parts.items():
            encoded, rung2_settings = _encode_stem_part(
                tensor,
                int(model.samplerate),
                codec=output_codec,
                mp3_settings=mp3_settings_rung1,
                source_bytes=source_bytes,
                source_kbps=source_kbps,
                out_path=tmp / f"{name}{ext}",
            )
            part_sizes[name] = len(encoded)
            if output_codec == "mp3":
                mp3_settings_by_part[name] = rung2_settings or mp3_settings_rung1  # type: ignore[assignment]

        violations = [
            dataclasses.asdict(v)
            for v in pol.assert_lossy_stems_not_larger_than_source(
                source_bytes=source_bytes, part_sizes=part_sizes, codec=output_codec
            )
        ]
        manifest = _manifest_dict(
            stable_id=stable_id,
            audio_path=audio_path,
            source_sha256=_sha256_file(audio_path),
            ext=ext,
            output_codec=output_codec,
            audio={
                "sample_rate": int(model.samplerate),
                "frame_count": frame_count,
                "channels": int(model.audio_channels),
            },
            size_policy={
                "source_bytes": source_bytes,
                "source_kbps": round(source_kbps, 2),
                "mp3_settings": mp3_settings_by_part or None,
                "part_bytes": part_sizes,
                "violations": violations,
                "policy_codec": policy_codec,
                "codec_fallback": codec_fallback,
            },
        )
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
        "codec": output_codec,
        "device_used": device_used,
        "wall_s": round(wall_s, 1),
        "realtime_factor": round(wall_s / source_duration_s, 3),
        "size_violations": violations,
        "codec_fallback": codec_fallback,
    }
    _log(
        f"[stem] done id={stable_id} wall={wall_s:.1f}s "
        f"rt={result['realtime_factor']}x device={device_used} codec={output_codec}"
    )
    return result


# ----- CLI -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Write a v3 htdemucs stem bundle")
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
    # This file is invoked as `uv run scripts/stem_bundle_worker.py ...`,
    # which puts THIS file's own directory on sys.path[0], not the repo root
    # -- `scripts` is not in pyproject.toml's editable-install package list
    # (only `apps*` is), so `from apps.stems import stem_size_policy`
    # (write_bundle, _stem_file_suffix, _encode_stem_part) would otherwise
    # raise ModuleNotFoundError. Must run before any of those local imports.
    sys.path.insert(0, str(REPO_ROOT))

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
