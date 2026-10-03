"""Modal.com spike: demucs vocal separation on a serverless L4 GPU.

SPIKE app (do NOT `modal run` without an authed Modal account -- there is
no auth yet). Authored only; verified by AST-parse + the Modal v1.x API
(modal.App / modal.Image / @app.function / @app.local_entrypoint / .map)
checked against the 2026 docs. See "API-doc sources" at the bottom.

What it does: ship one audio file's bytes to a cold or warm L4 container,
run htdemucs vocal separation (weights BAKED into the image at build time
so cold starts do not re-fetch), reduce the vocals/mix RMS envelope to the
SPIKE-B2 region JSON, and return a dict whose ``regions`` match the
``data/state/vocal-cache/*.json`` region shape EXACTLY
({start_s, end_s, confidence, intensity}) so results are import-compatible
with apps.vocals.cache. The region-extraction maths (thresholds, hop,
hysteresis, confidence gain, intensity ramp) are VENDORED VERBATIM from
scripts/vocal_region_worker.py + apps/vocals/cache.py -- not reinvented.

Requirements (mini-PRD):
  ✔︎ image bakes htdemucs weights at BUILD time (run_function download step)
    so a cold container does not re-fetch the ~80MB bag-of-models.
    [if] a cold container calls separate_track [then] no checkpoint download
    [if] the download step is removed [then ⛔️] cold latency includes fetch
  ✔︎ separate_track(audio_bytes, name) runs on gpu="L4", asserts cuda is
    available, and returns {name, regions, coverage, separate_s, device}
    with each region == {start_s, end_s, confidence, intensity} (cache shape).
    [if] cuda is unavailable inside the function [then ⛔️] AssertionError
    [if] a region key set != cache's 4 keys [then ⛔️] not import-compatible
  ✔︎ region maths reuse the calibrated SPIKE-B2 params (on 0.10 / off 0.05,
    hop 0.5s, merge < 1.5s, drop < 1.0s, confidence gain 2.5, intensity ramp)
    -- vendored, never re-tuned.
    [if] any threshold differs from vocal_region_worker.py [then ⛔️]
  ✔︎ drift guard: region seconds live in the SOURCE timeline; the resampled
    tensor duration must match ffprobe's source duration within tolerance.
    [if] resampled duration drifts > max(0.5s, 0.5%) [then ⛔️] refuse to emit
  ✔︎ local_entrypoint main(audio) picks a REAL existing library file when no
    --audio is given (state.db tracks -> known music dirs), fails fast listing
    every path it tried when none exist, and prints the timing breakdown
    (upload_s, separate_s, total_s) + writes JSON to
    .tmp/bench/modal_spike_out/<stable-name>.json.
    [if] no default audio file exists anywhere it looked [then ⛔️] RuntimeError
    listing the tracks-db probe count + globbed dirs
  ✔︎ fan(count) runs .map() over N copies of the file to test parallel
    scale-out (default 4), printing per-input separate_s + wall time.
    [if] --count 4 [then] 4 remote calls fan out, wall < 4 x single separate_s
  ✔︎ brittle fail-fast: no silent fallbacks, explicit errors everywhere.

Run (needs a Modal account + `modal token new`):
  modal run scripts/modal_vocal_spike.py
  modal run scripts/modal_vocal_spike.py --audio /path/to/track.mp3
  modal run scripts/modal_vocal_spike.py::fan --count 4

-Claude
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import modal

# ----- calibrated params (VENDORED from scripts/vocal_region_worker.py section
#       6.6 + apps/vocals/cache.py -- do NOT tune casually) --------------------
MODEL_NAME: str = "htdemucs"
HOP_S: float = 0.5            # RMS envelope hop
ON_RATIO: float = 0.10        # vocals-rms / mix-rms to enter a region
OFF_RATIO: float = 0.05       # ...to exit (hysteresis)
MERGE_GAP_S: float = 1.5      # merge regions separated by < this gap
MIN_REGION_S: float = 1.0     # drop merged regions shorter than this
CONFIDENCE_GAIN: float = 2.5  # confidence = min(1, GAIN * max ratio in region)
DRIFT_ABS_TOL_S: float = 0.5  # resampled duration must match source within...
DRIFT_REL_TOL: float = 0.005  # ...max(abs, rel * source) seconds

GPU_KIND: str = "L4"
TORCH_CACHE_DIR: str = "/root/.cache/torch"  # where get_model bakes weights
DEFAULT_FAN_COUNT: int = 4
OUT_DIR: Path = Path(__file__).resolve().parent.parent / ".tmp/bench/modal_spike_out"

# Candidate on-disk music roots probed when --audio is not given (the tracks
# table often carries a foreign checkout's absolute paths that do not resolve
# on this machine, so a real-file glob is the reliable fallback). An optional
# caller-selected audio directory is provided through MDT_OLTF_AUDIO_DIR; the
# compatibility environment name is retained. No private calibration corpus
# is included or accepted as release evidence by this script.
_OLTF_AUDIO_DIR: str = os.environ.get("MDT_OLTF_AUDIO_DIR", "")
_MUSIC_GLOB_DIRS: tuple[Path, ...] = (
    Path.home() / "Music/Manual Library",
    *((Path(_OLTF_AUDIO_DIR).expanduser(),) if _OLTF_AUDIO_DIR else ()),
    Path.home() / "Music/PioneerDJ",
    Path.home() / "Music",
)
_AUDIO_SUFFIXES: tuple[str, ...] = (".mp3", ".m4a", ".wav", ".flac", ".aac")
_STATE_DB: Path = Path(__file__).resolve().parent.parent / "data/state/state.db"


# ----- pure region maths (VENDORED VERBATIM; torch-free) ----------------------

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
            f"envelope length mismatch: vocals {len(vocals_rms)} "
            f"!= mix {len(mix_rms)}"
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
    """PoC confidence: clipped CONFIDENCE_GAIN x max ratio inside the region."""
    i0 = int(start_s / hop_s)
    i1 = max(i0 + 1, int(end_s / hop_s))
    values = [v for t, v in ratio_env[i0:i1]]
    if not values:
        raise ValueError(f"region [{start_s}, {end_s}] has no envelope frames")
    return round(min(1.0, CONFIDENCE_GAIN * max(values)), 2)


def intensity_of(confidence: float) -> int:
    """Map worker confidence (0..1) onto the PVDI intensity ramp (1..4).
    Verbatim port of apps/vocals/cache.py."""
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence out of range [0, 1]: {confidence}")
    return max(1, min(4, round(confidence * 4)))


def regions_from_envelope(
    ratio_env: list[tuple[float, float]], duration_s: float, hop_s: float = HOP_S
) -> tuple[list[dict[str, Any]], float]:
    """Cache-shaped regions ({start_s, end_s, confidence, intensity}) + coverage%.

    The intensity field is what makes the output import-compatible with the
    apps.vocals.cache contract (which validates set(region) == those 4 keys).
    """
    if duration_s <= 0:
        raise ValueError(f"non-positive duration_s: {duration_s}")
    spans = envelope_to_regions(ratio_env, on=ON_RATIO, off=OFF_RATIO)
    regions: list[dict[str, Any]] = []
    for s, e in spans:
        confidence = region_confidence(ratio_env, s, e, hop_s)
        regions.append(
            {
                "start_s": s,
                "end_s": e,
                "confidence": confidence,
                "intensity": intensity_of(confidence),
            }
        )
    coverage = 100.0 * sum(e - s for s, e in spans) / duration_s
    return regions, round(coverage, 1)


# ----- Modal image: htdemucs weights baked at build time ----------------------

def _download_weights() -> None:
    """Build step: fetch the htdemucs bag-of-models into the image filesystem
    so a cold container never re-downloads it. Runs during `modal run` build."""
    from demucs.pretrained import get_model

    model = get_model(MODEL_NAME)
    model.eval()
    print(f"[build] baked {MODEL_NAME} weights into {TORCH_CACHE_DIR}")


image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install(
        "demucs==4.0.1",
        "torch==2.5.1",
        "torchaudio==2.5.1",
        "soundfile>=0.12",
        "numpy<2",
    )
    .run_function(_download_weights)
)

app = modal.App(name="mdt-vocal-spike", image=image)


# ----- remote GPU function ----------------------------------------------------

def _rms_envelope(wav: Any, sr: int, hop_s: float) -> list[float]:
    mono = wav.mean(dim=0)
    hop = int(sr * hop_s)
    n = len(mono) // hop
    return [
        float(mono[i * hop:(i + 1) * hop].pow(2).mean().sqrt()) for i in range(n)
    ]


def _separate_vocals(model: Any, wav: Any, device: str) -> Any:
    from demucs.apply import apply_model

    ref = wav.mean(0)
    normed = (wav - ref.mean()) / ref.std()
    sources = apply_model(model, normed[None], device=device, progress=False)[0]
    vocals = sources[model.sources.index("vocals")]
    return vocals * ref.std() + ref.mean()


@app.function(gpu=GPU_KIND, timeout=600)
def separate_track(audio_bytes: bytes, name: str) -> dict[str, Any]:
    """htdemucs vocal separation on an L4, reduced to cache-shaped regions.

    Fails fast (no CPU fallback in the spike): cuda must be available and the
    resampled tensor must sit on the source timeline, else the call raises.
    """
    import tempfile

    import torch
    from demucs.audio import AudioFile
    from demucs.pretrained import get_model

    assert torch.cuda.is_available(), "cuda unavailable inside the L4 container"
    device = "cuda"

    model = get_model(MODEL_NAME)
    model.eval()

    # demucs.audio.AudioFile shells to ffmpeg/ffprobe, which need a real path.
    suffix = Path(name).suffix or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(audio_bytes)
        audio_path = Path(handle.name)

    audio_file = AudioFile(audio_path)
    source_sr = int(audio_file.samplerate())
    source_duration_s = float(audio_file.duration)
    wav = audio_file.read(
        streams=0, samplerate=model.samplerate, channels=model.audio_channels
    )

    # Drift guard (NOTE-musicbot-alignment): region seconds are computed on the
    # resampled tensor; that is only the source timeline if duration survived.
    resampled_duration_s = wav.shape[-1] / model.samplerate
    tolerance = max(DRIFT_ABS_TOL_S, DRIFT_REL_TOL * source_duration_s)
    if abs(resampled_duration_s - source_duration_s) > tolerance:
        raise RuntimeError(
            f"sample-rate drift for {name}: source {source_duration_s:.2f}s @ "
            f"{source_sr} Hz but resampled tensor is {resampled_duration_s:.2f}s "
            f"@ {model.samplerate} Hz (tolerance {tolerance:.2f}s)."
        )

    t0 = time.perf_counter()
    vocals = _separate_vocals(model, wav, device)
    separate_s = time.perf_counter() - t0

    sr = model.samplerate
    ratio = ratio_envelope(
        _rms_envelope(vocals, sr, HOP_S), _rms_envelope(wav, sr, HOP_S), HOP_S
    )
    regions, coverage = regions_from_envelope(ratio, source_duration_s)

    return {
        "name": name,
        "regions": regions,
        "coverage": coverage,
        "separate_s": round(separate_s, 1),
        "device": device,
    }


# ----- default-audio resolution (local; fail-fast) ----------------------------

def _is_audio(path: Path) -> bool:
    return path.suffix.lower() in _AUDIO_SUFFIXES


def _tracks_db_candidates() -> tuple[list[str], int]:
    """Existing on-disk file_paths from state.db, plus how many rows were probed
    (so a fail-fast message can say what it looked at)."""
    if not _STATE_DB.is_file():
        return [], 0
    connection = sqlite3.connect(f"file:{_STATE_DB}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT file_path FROM tracks "
            "WHERE file_path IS NOT NULL AND file_path != ''"
        ).fetchall()
    finally:
        connection.close()
    existing = [row[0] for row in rows if Path(row[0]).is_file()]
    return existing, len(rows)


def _resolve_default_audio() -> Path:
    """Pick a real existing library file, or raise listing every probe."""
    db_hits, db_rows = _tracks_db_candidates()
    if db_hits:
        return Path(db_hits[0])
    if _OLTF_AUDIO_DIR and not Path(_OLTF_AUDIO_DIR).expanduser().is_dir():
        raise RuntimeError(f"MDT_OLTF_AUDIO_DIR is set but is not a directory: {_OLTF_AUDIO_DIR}")
    for directory in _MUSIC_GLOB_DIRS:
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.rglob("*")):
            if candidate.is_file() and _is_audio(candidate):
                print(f"[modal-spike] no --audio: using {candidate}", flush=True)
                return candidate
    tried_dirs = ", ".join(str(directory) for directory in _MUSIC_GLOB_DIRS)
    raise RuntimeError(
        "no default audio file found. Probed state.db "
        f"({_STATE_DB}: {db_rows} track rows, 0 existing on disk) and globbed "
        f"[{tried_dirs}] for {_AUDIO_SUFFIXES}. Pass --audio <path> explicitly."
    )


def _resolve_audio_arg(audio: str) -> Path:
    if audio:
        path = Path(audio).expanduser()
        if not path.is_file():
            raise RuntimeError(f"--audio path does not exist: {path}")
        if not _is_audio(path):
            raise RuntimeError(f"--audio is not an audio file: {path}")
        return path
    return _resolve_default_audio()


def _write_result(name: str, result: dict[str, Any]) -> Path:
    import json

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{Path(name).stem}.json"
    out_path.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return out_path


# ----- local entrypoints ------------------------------------------------------

@app.local_entrypoint()
def main(audio: str = "") -> None:
    """Single-file run: upload one track, separate on L4, print timings."""
    audio_path = _resolve_audio_arg(audio)
    audio_bytes = audio_path.read_bytes()
    name = audio_path.name
    print(f"audio={audio_path} bytes={len(audio_bytes)}")

    wall0 = time.perf_counter()
    result = separate_track.remote(audio_bytes, name)
    total_s = time.perf_counter() - wall0

    separate_s = result["separate_s"]
    upload_s = round(total_s - separate_s, 1)  # wall minus GPU compute
    out_path = _write_result(name, result)
    print(
        f"device={result['device']} regions={len(result['regions'])} "
        f"coverage={result['coverage']}%"
    )
    print(
        f"timing: upload+overhead={upload_s}s separate_s={separate_s}s "
        f"total_s={round(total_s, 1)}s"
    )
    print(f"wrote {out_path}")


@app.local_entrypoint()
def fan(audio: str = "", count: int = DEFAULT_FAN_COUNT) -> None:
    """Parallel scale-out: .map() the same track over N containers."""
    if count < 1:
        raise ValueError(f"--count must be >= 1, got {count}")
    audio_path = _resolve_audio_arg(audio)
    audio_bytes = audio_path.read_bytes()
    names = [f"{audio_path.stem}.copy{i}{audio_path.suffix}" for i in range(count)]
    print(f"audio={audio_path} bytes={len(audio_bytes)} fan_count={count}")

    wall0 = time.perf_counter()
    results = list(separate_track.map([audio_bytes] * count, names))
    wall_s = time.perf_counter() - wall0

    for result in results:
        print(
            f"  {result['name']}: separate_s={result['separate_s']}s "
            f"regions={len(result['regions'])} coverage={result['coverage']}%"
        )
    slowest = max(result["separate_s"] for result in results)
    print(
        f"fan wall_s={round(wall_s, 1)}s over {count} inputs "
        f"(slowest separate_s={slowest}s -> scale-out speedup vs serial "
        f"~{round(sum(r['separate_s'] for r in results) / wall_s, 1)}x)"
    )


# ----- API-doc sources checked (Modal v1.x, 2026 docs via context7) -----------
#   modal.App(name=, image=)                 modal.com/docs/sdk/py/latest/modal.App
#   modal.Image.debian_slim().apt_install()  modal.com/docs/guide/images
#     .pip_install(...).run_function(fn)      (run_function bakes weights at build)
#   @app.function(gpu="L4", timeout=)        modal.com/docs/guide/gpu
#   fn.remote(*args)                          modal.com/docs/sdk/py/latest/modal.Function
#   fn.map(iter_a, iter_b)                    parallel scale-out, order preserved
#   @app.local_entrypoint()                  primitive args -> CLI flags (str/int)
