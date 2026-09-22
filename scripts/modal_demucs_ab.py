#!/usr/bin/env python3
"""Modal H100 runner: htdemucs_ft comparison stems for the roformer A/B spike.

Sibling of ``scripts/modal_roformer_spike.py`` (Modal image/Volume pattern)
and ``scripts/modal_vocal_farm.py`` (htdemucs_ft is already in its
BAKED_MODELS, and this runner's ``_separate_two_stems`` is the same
apply_model + affine-denormalize shape as that file's ``_separate_all``,
narrowed to two outputs). This script exists because neither sibling
produces exactly {vocals, instrumental} for a fixed 10-track list into a
NEW directory -- the farm writes all four demucs sources to R2/bifrost2/
data/state/stems, and the roformer spike is a different model family
writing into data/state/stems-roformer-spike. Both of those are
out of bounds for this comparison render (house rule: never touch
data/state/stems/ or the roformer-spike dir; this dir is append-only).

TWO-STEMS SEMANTICS. "instrumental" is drums+bass+other summed AFTER each
already carries its own ``* ref.std() + ref.mean()`` denormalization -- this
matches facebookresearch/demucs's own ``separate.py --two-stems`` default
combine method (``--other-method add``: ``other_stem = zeros; for i in
res.values(): other_stem += i`` over the per-source tensors, not a single
affine pass over the raw summed sources). Verified against that file's
source before writing this comment, rather than assumed. ref.mean() being
added len(INSTRUMENTAL_SOURCES) times instead of once is therefore the
CORRECT reproduction of what demucs itself ships, not a bug to "fix".

CONFIG: model=htdemucs_ft, overlap=0.25, shifts=0 -- the exact knobs of
apps/stems/tiers.py's Tier "L" ("htdemucs_ft-ov0.25"), the same config the
tier configuration. Private listening scores are not included in the
public source. Keep these knobs fixed when comparing model families so
the configuration is explicit; this text makes no quality acceptance claim.

OUTPUT POLICY: 16-bit PCM FLAC for both stems, unconditionally. Every
source track here is a lossy MP3 (no PCM bit depth of its own -- see the
roformer spike's meta.json note for the same tracks), and PCM_16 is the
existing house convention for durable stem FLACs (stem_bundle_worker.py,
modal_vocal_farm.py's ``_encode_flac``): "control/eval FLAC output is
always 16-bit" already, not a per-source decision. No wider encode is ever
attempted, so there is nothing to assert against here (contrast the
roformer spike, which DOES assert because audio-separator can emit a wider
subtype than requested).

Requirements (mini-PRD):
  ✔︎ ✅ one Modal call per track returns BOTH vocals.flac and
    instrumental.flac (2-stem, not 4-stem) at htdemucs_ft/ov0.25/shifts0.
    [if] the container has no CUDA device [then ⛔️] raise before any compute
  ✔︎ ✅ outputs land ONLY under data/state/stems-demucs-ab/<stable_id>/,
    never under data/state/stems/ or data/state/stems-roformer-spike/.
    [if] --out-dir is not passed [then] the default is stems-demucs-ab
  ✔︎ ✅ a bad track's exception is isolated ({error} return), never aborts
    the batch; the driver still exits non-zero if any track failed.
    [if] 9/10 tracks succeed [then] 9 dirs are written and exit code is 1
  ✔︎ ✅ every track's meta.json stamps model/overlap/shifts/gpu_kind/
    source_path/timings, mirroring the roformer spike's provenance shape.

Run (modal + torch never enter the repo venv -- overlay both):
  uv run --with modal python scripts/modal_demucs_ab.py roformer-spike-set
  uv run --with modal python scripts/modal_demucs_ab.py roformer-spike-set --limit 2

-Claude
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import modal

# ----- config ----------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
ROFORMER_SPIKE_DIR: Path = REPO_ROOT / "data" / "state" / "stems-roformer-spike"
DEFAULT_OUT_DIR: Path = REPO_ROOT / "data" / "state" / "stems-demucs-ab"

GPU_KIND: str = "H100"  # constraint: Modal H100 for all separation, never bifrost2
TASK_TIMEOUT_S: int = 900
MAX_CONTAINERS: int = 10  # one per track -- true parallelism across a 10-track batch

MODEL_NAME: str = "htdemucs_ft"
OVERLAP: float = 0.25  # apps/stems/tiers.py Tier "L" (htdemucs_ft-ov0.25)
SHIFTS: int = 0
VOCALS_SOURCE: str = "vocals"
INSTRUMENTAL_SOURCES: tuple[str, ...] = ("drums", "bass", "other")
CONFIG_TAG: str = f"{MODEL_NAME}-ov{OVERLAP}"

TORCH_CACHE_DIR: str = "/root/.cache/torch"


def _download_weights() -> None:
    """Build step: bake htdemucs_ft weights into the image so a cold
    container never re-fetches them. Changing MODEL_NAME rebuilds once."""
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

app = modal.App(name="mdt-demucs-ab", image=image)


# ----- remote GPU function -----------------------------------------------------


def _separate_two_stems(audio_bytes: bytes, name: str) -> dict[str, Any]:
    """One track, IN THE CONTAINER: vocals.flac + instrumental.flac bytes.

    See module docstring "TWO-STEMS SEMANTICS" for why the instrumental sum
    happens AFTER each source's own denormalization, not before.
    """
    import io
    import tempfile

    import numpy as np
    import soundfile as sf
    import torch
    from demucs.apply import apply_model
    from demucs.audio import AudioFile
    from demucs.pretrained import get_model

    if not torch.cuda.is_available():
        raise RuntimeError("cuda unavailable inside the H100 container")

    model = get_model(MODEL_NAME)
    model.eval()

    # demucs.audio.AudioFile shells to ffmpeg/ffprobe, which need a real path.
    suffix = Path(name).suffix or ".mp3"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(audio_bytes)
        audio_path = Path(handle.name)

    load0 = time.perf_counter()
    audio_file = AudioFile(audio_path)
    wav = audio_file.read(
        streams=0, samplerate=model.samplerate, channels=model.audio_channels
    )
    load_s = time.perf_counter() - load0

    sep0 = time.perf_counter()
    ref = wav.mean(0)
    normed = (wav - ref.mean()) / ref.std()
    sources = apply_model(
        model, normed[None], device="cuda", overlap=OVERLAP, shifts=SHIFTS, progress=False
    )[0]
    denorm = {
        src_name: sources[model.sources.index(src_name)] * ref.std() + ref.mean()
        for src_name in model.sources
    }
    separate_s = time.perf_counter() - sep0

    if VOCALS_SOURCE not in denorm:
        raise RuntimeError(f"{MODEL_NAME} has no {VOCALS_SOURCE!r} source: {model.sources}")
    vocals = denorm[VOCALS_SOURCE]
    instrumental = torch.zeros_like(vocals)
    for src_name in INSTRUMENTAL_SOURCES:
        if src_name not in denorm:
            raise RuntimeError(f"{MODEL_NAME} has no {src_name!r} source: {model.sources}")
        instrumental += denorm[src_name]

    def _encode_flac16(tensor: Any) -> bytes:
        arr = tensor.detach().cpu().numpy().T.astype(np.float32, copy=False)
        if arr.ndim != 2 or arr.shape[1] not in (1, 2):
            raise RuntimeError(f"unexpected stem shape {arr.shape}")
        buffer = io.BytesIO()
        sf.write(buffer, arr, model.samplerate, format="FLAC", subtype="PCM_16")
        return buffer.getvalue()

    return {
        "vocals": _encode_flac16(vocals),
        "instrumental": _encode_flac16(instrumental),
        "samplerate": model.samplerate,
        "timings": {"load_s": round(load_s, 2), "separate_s": round(separate_s, 2)},
    }


@app.function(gpu=GPU_KIND, timeout=TASK_TIMEOUT_S, max_containers=MAX_CONTAINERS)
def separate_track(audio_bytes: bytes, name: str, stable_id: str, source_path: str) -> dict[str, Any]:
    """One track: separate on an H100. Returns ``{stable_id, error}`` instead
    of raising, so one bad file cannot abort the whole 10-track batch."""
    entered = time.perf_counter()
    try:
        result = _separate_two_stems(audio_bytes, name)
    except Exception as exc:  # per-track isolation is the whole point
        return {
            "stable_id": stable_id,
            "error": f"{type(exc).__name__}: {exc}",
            "gpu_s": round(time.perf_counter() - entered, 2),
        }
    result["stable_id"] = stable_id
    result["source_path"] = source_path
    result["gpu_s"] = round(time.perf_counter() - entered, 2)
    return result


# ----- local: driver ------------------------------------------------------


def _roformer_spike_tracks() -> list[tuple[str, Path]]:
    """(stable_id, source_path) for every dir in data/state/stems-roformer-spike --
    read-only, matches the task's own instruction to source the 10 tracks
    from those meta.json files."""
    if not ROFORMER_SPIKE_DIR.is_dir():
        raise SystemExit(f"error: missing {ROFORMER_SPIKE_DIR}")
    tracks: list[tuple[str, Path]] = []
    for meta_path in sorted(ROFORMER_SPIKE_DIR.glob("*/meta.json")):
        meta = json.loads(meta_path.read_text())
        stable_id = meta["stable_id"]
        source_path = Path(meta["source_path"])
        if not source_path.is_file():
            raise SystemExit(f"error: source audio missing for {stable_id}: {source_path}")
        tracks.append((stable_id, source_path))
    if not tracks:
        raise SystemExit(f"error: no meta.json found under {ROFORMER_SPIKE_DIR}")
    return tracks


def cmd_roformer_spike_set(args: argparse.Namespace) -> None:
    tracks = _roformer_spike_tracks()
    if args.limit:
        tracks = tracks[: args.limit]
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"[OK] config_tag={CONFIG_TAG} model={MODEL_NAME} overlap={OVERLAP} "
        f"shifts={SHIFTS} tracks={len(tracks)} out_dir={out_dir}",
        flush=True,
    )

    wall0 = time.perf_counter()
    with app.run():
        remote_results = list(
            separate_track.starmap(
                [
                    (source_path.read_bytes(), source_path.name, stable_id, str(source_path))
                    for stable_id, source_path in tracks
                ]
            )
        )
    wall_s = time.perf_counter() - wall0

    failures: dict[str, str] = {}
    for (stable_id, source_path), result in zip(tracks, remote_results, strict=False):
        if "error" in result:
            failures[stable_id] = result["error"]
            print(f"[FAIL] {stable_id} ({source_path.name}): {result['error']}", flush=True)
            continue
        track_dir = out_dir / stable_id
        track_dir.mkdir(parents=True, exist_ok=True)
        (track_dir / "vocals.flac").write_bytes(result["vocals"])
        (track_dir / "instrumental.flac").write_bytes(result["instrumental"])
        meta = {
            "config_tag": CONFIG_TAG,
            "model": MODEL_NAME,
            "overlap": OVERLAP,
            "shifts": SHIFTS,
            "gpu_kind": GPU_KIND,
            "stable_id": stable_id,
            "source_path": str(source_path),
            "source_name": source_path.name,
            "samplerate": result["samplerate"],
            "vocals_bytes": len(result["vocals"]),
            "instrumental_bytes": len(result["instrumental"]),
            "timings": result["timings"],
            "gpu_s": result["gpu_s"],
        }
        (track_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        print(
            f"[OK] {stable_id} ({source_path.name}): "
            f"vocals={len(result['vocals'])}B instrumental={len(result['instrumental'])}B "
            f"gpu_s={result['gpu_s']:.1f}",
            flush=True,
        )

    print(f"\n[DONE] {len(tracks) - len(failures)}/{len(tracks)} ok wall_s={wall_s:.1f}", flush=True)
    if failures:
        raise SystemExit(f"error: {len(failures)}/{len(tracks)} tracks failed: {failures}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    spike_set = sub.add_parser(
        "roformer-spike-set",
        help="render vocals+instrumental for every data/state/stems-roformer-spike track",
    )
    spike_set.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    spike_set.add_argument("--limit", type=int, default=0, help="only the first N tracks (0 = all)")
    spike_set.set_defaults(func=cmd_roformer_spike_set)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
