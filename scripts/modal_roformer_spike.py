#!/usr/bin/env python3
"""Modal H100 runner for Mel-Band RoFormer vocal separation (roformer spike).

Sibling of ``scripts/modal_vocal_farm.py`` for a different model family. Where
the farm bakes demucs (torch, apply_model) into its image, this bakes
``python-audio-separator`` (torch + onnxruntime-gpu) with the default
Mel-Band RoFormer vocal checkpoint pre-downloaded, so a cold container never
pays a checkpoint download.

WHY audio-separator OVER ZFTurbo Music-Source-Separation-Training: both wrap
the same underlying Mel-Band RoFormer architecture (the MDXC code path in
audio-separator IS the roformer inference code, ported from ZFTurbo's
training repo). audio-separator won on three counts, checked locally before
committing to it (see .planning/roformer-spike-plan.md):
  * ``pip install audio-separator[gpu]`` resolves cleanly with modern torch
    (2.13 locally) with no CUDA-generation pin fight, unlike demucs' torch
    2.5.1/cu124 ceiling (see modal_vocal_farm.py's Blackwell comment) --
    H100 (sm_90) is comfortably inside its support window.
  * A CLI-first ``Separator`` class with a documented, versioned model
    registry (``--list_models``) is exactly the reproducibility surface a
    spike needs: a checkpoint name IS the config, no YAML-hunting on GitHub.
  * It downloads the checkpoint AND the matching architecture YAML together
    (``Separator.load_model``), which is what makes the "bake at image build
    time" pattern below a one-liner.

WHAT audio-separator DOES NOT GIVE UP: Test-Time-Augmentation. Its MDXC
architecture (which both BS-RoFormer and Mel-Band RoFormer run through) has
no TTA knob -- only the older VR architecture exposes ``--vr_enable_tta``.
Checked directly against the installed CLI's ``--help`` before writing this
comment. ``--tta`` on this runner therefore FAILS FAST rather than silently
running without it; see ``_resolve_config``. If a later round genuinely needs
roformer TTA, ZFTurbo's own ``inference.py`` (``--use_tta``) is the fallback,
at the cost of hand-rolling the reproducibility this runner gets for free.

DEFAULT CHECKPOINT: ``model_mel_band_roformer_ep_3005_sdr_11.4360.ckpt``,
listed by audio-separator as "Mel-Roformer-Viperx-1143" -- the original
ZFTurbo/viperx Mel-Band RoFormer checkpoint the architecture is best known
by, and the one ``.planning/roformer-spike-plan.md`` step 1 calls "the
default mel-band vocal checkpoint". Only this checkpoint is baked into the
image for now (BAKED_CHECKPOINTS); add rungs there and rebuild for
alternates (Kim/unwa lineage, BS-RoFormer) in a later iteration round.

Output policy (house rule, apps/stems/stem_size_policy.py's sibling rule for
this spike): 2-stem FLAC at the SOURCE bit depth. audio-separator already
matches output bit depth to input by default (checked locally: a 16-bit PCM
input produced 16-bit PCM output); ``_separate_one`` asserts this in-container
rather than trusting it, so a library upgrade that changes the default fails
the run instead of silently writing oversized stems.

Every run persists to the ``roformer-spike`` Modal Volume (durable, pullable
independently of this Mac) AND writes into ``--out-dir`` locally in the same
call. ``pull`` re-fetches from the Volume on demand without spending GPU time
again.

Requirements (mini-PRD):
  ✔︎ ✅ image bakes audio-separator + the default Mel-Band RoFormer checkpoint;
    a cold container never downloads weights.
    [if] BAKED_CHECKPOINTS changes [then] one image rebuild, cached forever after
  ✔︎ ✅ separate_track returns BOTH vocals.flac and instrumental.flac from one
    Separator.separate() call (2-stem, not 4-stem).
    [if] the checkpoint's config lacks a Vocals+Instrumental pair [then ⛔️] raise
  ✔︎ ✅ every output is written to the Volume under
    <config_tag>/<slug>/{vocals,instrumental}.flac + meta.json, AND returned to
    the caller so ``bench``/``separate`` can also write --out-dir.
    [if] the Volume write or commit fails [then ⛔️] the track raises, not silently drops
  ✔︎ ✅ config_tag is reproducible: checkpoint + overlap + segment_size +
    override_model_segment_size + pitch_shift + tta are all stamped into
    meta.json, and a default config_tag is DERIVED from them so two runs at
    identical knobs collide on purpose (append-only ledger catches a re-run).
  ✔︎ ✅ --tta refuses outright (see WHAT audio-separator DOES NOT GIVE UP above)
    rather than running without augmentation and reporting it as requested.
    [if] --tta is passed [then ⛔️] SystemExit naming why, before any GPU spend
  ✔︎ ✅ output bit depth never exceeds the source's.
    [if] a 16-bit source produces a wider-than-16-bit stem [then ⛔️] raise
    in-container, before the bytes cross the wire

Run:
  uv run --with modal python scripts/modal_roformer_spike.py bench --limit 1
  uv run --with modal python scripts/modal_roformer_spike.py bench
  uv run --with modal python scripts/modal_roformer_spike.py separate \
      --input some.flac --out-dir /tmp/out
  uv run --with modal python scripts/modal_roformer_spike.py pull \
      --config-tag melband-viperx1143-ov8-seg256 --out-dir /tmp/pulled

-Claude
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import modal

# ----- config -------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
BENCH_DIR: Path = REPO_ROOT / "scripts" / "bench"
CLIPS_MUSDB_DIR: Path = BENCH_DIR / "clips-musdb"
DEFAULT_OUT_DIR: Path = BENCH_DIR / "clips-roformer-spike"
LEDGER_PATH: Path = BENCH_DIR / "roformer_ledger.json"

GPU_KIND: str = "H100"  # constraint: Modal H100 for all separation, never bifrost2
TASK_TIMEOUT_S: int = 900
DEFAULT_MAX_CONTAINERS: int = 6  # 6-track baseline never needs more than one per track

AUDIO_SEPARATOR_VERSION: str = "0.44.5"  # pinned to what was probed locally

# Checkpoint filenames are audio-separator's own registry keys (see its
# --list_models). Only DEFAULT_CHECKPOINT is baked into the image; add more
# here (and to BAKED_CHECKPOINTS) for a later alternate-checkpoint round.
DEFAULT_CHECKPOINT: str = "model_mel_band_roformer_ep_3005_sdr_11.4360.ckpt"
BAKED_CHECKPOINTS: tuple[str, ...] = (DEFAULT_CHECKPOINT,)

# MDXC architecture defaults (audio-separator's own Separator() defaults,
# not tuned here -- "default knobs" per the spike plan's step 3). MDXC
# overlap is an INTEGER window-overlap count (2-50), not the demucs-style
# 0..1 fraction; the two are not on the same scale, so "overlap 0.25
# equivalent" from the task brief is honoured as "run this arch's own
# default", not a numeric conversion. Recorded honestly in the ledger note.
DEFAULT_OVERLAP: int = 8
DEFAULT_SEGMENT_SIZE: int = 256
DEFAULT_OVERRIDE_SEGMENT_SIZE: bool = False
DEFAULT_PITCH_SHIFT: int = 0

MODEL_CACHE_DIR: str = "/root/.cache/audio-separator"
VOLUME_NAME: str = "roformer-spike"
VOLUME_MOUNT_PATH: str = "/vol"

# htdemucs_ft per-track SI-SDR (dB), copied verbatim from the 6-track table in
# scripts/bench/MODEL-SHOOTOUT.md (Thu 24 Jul 2026 run, GTX 1660, overlap
# 0.25, shifts 0). Keyed by shootout_spec.Track.slug so a paired delta needs
# no fuzzy matching. Do NOT hand-edit MODEL-SHOOTOUT.md's own tables to add a
# roformer column -- it is generated by shootout_report.py and a hand edit
# dies on regen; this dict is this script's own copy of the numbers it needs.
HTDEMUCS_FT_SISDR: dict[str, float] = {
    "al-james-schoolboy-facination": 7.05,
    "zeno-signs": 8.96,
    "timboz-pony": 4.19,
    "sambasevam-shanmugam-kaathaadi": 16.47,
    "enda-reilly-cur-an-long-ag-seol": 13.44,
    "cristina-vane-so-easy": 10.08,
}
HTDEMUCS_FT_MEDIAN_SISDR: float = 9.52  # MODEL-SHOOTOUT.md headline

# Goal from .planning/roformer-spike-plan.md: beat htdemucs_ft by >= this
# median paired delta to count as a win worth pursuing further. This is a
# DIFFERENT threshold from the +0.2 dB "accept a lever change vs the
# previous roformer round" rule in the same plan -- that rule has no
# previous round to compare against on a first baseline run, so it is not
# applied here. Recorded explicitly in the ledger note per entry.
GOAL_MEDIAN_PAIRED_DELTA_DB: float = 1.0


def _runner_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


# ----- Modal image and app --------------------------------------------------


def _bake_checkpoints() -> None:
    """Build step: download every BAKED_CHECKPOINTS file + its arch YAML into
    the image filesystem so a cold container never fetches weights.

    CHANGING BAKED_CHECKPOINTS REBUILDS THE IMAGE ONCE. Modal keys the layer
    on this function's closure (BAKED_CHECKPOINTS), so adding a rung here is
    a deliberate one-off rebuild, not an accident on every deploy.
    """
    from audio_separator.separator import Separator

    sep = Separator(model_file_dir=MODEL_CACHE_DIR, output_dir="/tmp/bake-out")
    for checkpoint in BAKED_CHECKPOINTS:
        sep.load_model(checkpoint)
        print(f"[build] baked {checkpoint} into {MODEL_CACHE_DIR}")


image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install(f"audio-separator[gpu]=={AUDIO_SEPARATOR_VERSION}")
    .run_function(_bake_checkpoints)
)

app = modal.App(name="mdt-roformer-spike", image=image)
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)


# ----- remote GPU function ---------------------------------------------------


def _separate_one(
    audio_bytes: bytes,
    name: str,
    checkpoint: str,
    overlap: int,
    segment_size: int,
    override_segment_size: bool,
    pitch_shift: int,
) -> dict[str, Any]:
    """One track, both stems, IN THE CONTAINER. Raises on anything unexpected;
    the caller converts a raise into a per-track failure so one bad file
    cannot abort a small batch (mirrors modal_vocal_farm.py's per-track
    isolation, at 1/6th the scale this spike actually needs)."""
    import tempfile

    import soundfile as sf
    from audio_separator.separator import Separator

    suffix = Path(name).suffix or ".flac"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        in_path = tmp_path / f"input{suffix}"
        in_path.write_bytes(audio_bytes)
        source_info = sf.info(str(in_path))

        sep = Separator(
            model_file_dir=MODEL_CACHE_DIR,
            output_dir=str(tmp_path),
            output_format="FLAC",
            mdxc_params={
                "segment_size": segment_size,
                "override_model_segment_size": override_segment_size,
                "batch_size": 1,
                "overlap": overlap,
                "pitch_shift": pitch_shift,
            },
        )
        load0 = time.perf_counter()
        sep.load_model(checkpoint)
        load_s = time.perf_counter() - load0

        sep0 = time.perf_counter()
        out_names = sep.separate(str(in_path))
        separate_s = time.perf_counter() - sep0

        vocals_name = next((n for n in out_names if "(Vocals)" in n), None)
        inst_name = next((n for n in out_names if "(Instrumental)" in n), None)
        if vocals_name is None or inst_name is None:
            raise RuntimeError(
                f"{checkpoint} did not produce a Vocals+Instrumental pair: {out_names}"
            )
        vocals_path = tmp_path / vocals_name
        inst_path = tmp_path / inst_name

        for part_path in (vocals_path, inst_path):
            out_info = sf.info(str(part_path))
            if out_info.subtype != source_info.subtype:
                raise RuntimeError(
                    f"bit-depth policy violated for {name}: source subtype "
                    f"{source_info.subtype!r}, {part_path.name} is "
                    f"{out_info.subtype!r}. Separation adds no information, "
                    "so output must never carry more bits than the source "
                    "(apps/stems/stem_size_policy.py's rule for this spike)."
                )

        return {
            "vocals": vocals_path.read_bytes(),
            "instrumental": inst_path.read_bytes(),
            "source_subtype": source_info.subtype,
            "source_samplerate": source_info.samplerate,
            "timings": {"load_s": round(load_s, 2), "separate_s": round(separate_s, 2)},
        }


@app.function(
    gpu=GPU_KIND,
    timeout=TASK_TIMEOUT_S,
    max_containers=DEFAULT_MAX_CONTAINERS,
    volumes={VOLUME_MOUNT_PATH: volume},
)
def separate_track(
    audio_bytes: bytes,
    name: str,
    slug: str,
    checkpoint: str,
    overlap: int,
    segment_size: int,
    override_segment_size: bool,
    pitch_shift: int,
    config_tag: str,
    source_path: str,
) -> dict[str, Any]:
    """One track: separate on an H100, persist to the Volume, return bytes.

    Returns ``{slug, error}`` instead of raising so one bad track cannot
    abort the whole batch -- the same per-track isolation modal_vocal_farm.py
    uses, kept even though this spike's batches are tiny.
    """
    import torch

    entered = time.perf_counter()
    if not torch.cuda.is_available():
        raise RuntimeError("cuda unavailable inside the H100 container")
    try:
        result = _separate_one(
            audio_bytes, name, checkpoint, overlap, segment_size, override_segment_size, pitch_shift
        )
    except Exception as exc:  # per-track isolation is the whole point
        return {
            "slug": slug,
            "error": f"{type(exc).__name__}: {exc}",
            "gpu_s": round(time.perf_counter() - entered, 2),
        }

    vol_dir = Path(VOLUME_MOUNT_PATH) / config_tag / slug
    vol_dir.mkdir(parents=True, exist_ok=True)
    (vol_dir / "vocals.flac").write_bytes(result["vocals"])
    (vol_dir / "instrumental.flac").write_bytes(result["instrumental"])
    meta = {
        "config_tag": config_tag,
        "checkpoint": checkpoint,
        "overlap": overlap,
        "segment_size": segment_size,
        "override_model_segment_size": override_segment_size,
        "pitch_shift": pitch_shift,
        "tta": False,
        "gpu_kind": GPU_KIND,
        "slug": slug,
        "source_path": source_path,
        "source_name": name,
        "source_subtype": result["source_subtype"],
        "source_samplerate": result["source_samplerate"],
        "vocals_bytes": len(result["vocals"]),
        "instrumental_bytes": len(result["instrumental"]),
        "timings": result["timings"],
        "runner_sha256": _runner_sha256(),
    }
    (vol_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    volume.commit()

    return {
        "slug": slug,
        "vocals": result["vocals"],
        "instrumental": result["instrumental"],
        "meta": meta,
        "gpu_s": round(time.perf_counter() - entered, 2),
    }


# ----- local: config + shared plumbing --------------------------------------


@dataclass(frozen=True)
class RunConfig:
    checkpoint: str
    overlap: int
    segment_size: int
    override_segment_size: bool
    pitch_shift: int
    config_tag: str


def _resolve_config(args: argparse.Namespace) -> RunConfig:
    if args.tta:
        raise SystemExit(
            "error: --tta requested, but Mel-Band RoFormer runs through "
            "audio-separator's MDXC architecture, which has NO Test-Time-"
            "Augmentation knob (only the older VR architecture exposes "
            "--vr_enable_tta; checked against `audio-separator --help` "
            "before writing this runner). Refusing rather than silently "
            "running without augmentation and reporting it as requested. "
            "See the module docstring for the ZFTurbo --use_tta fallback."
        )
    checkpoint = args.checkpoint
    if checkpoint not in BAKED_CHECKPOINTS:
        raise SystemExit(
            f"error: checkpoint {checkpoint!r} is not baked into the image "
            f"(baked: {', '.join(BAKED_CHECKPOINTS)}). A checkpoint that costs "
            "a cold per-container download is a checkpoint nobody should run "
            "at scale; add it to BAKED_CHECKPOINTS and rebuild first."
        )
    checkpoint_short = checkpoint.split("_ep_")[0].replace("model_", "").replace("_", "-")
    default_tag = (
        f"{checkpoint_short}-ov{args.overlap}-seg{args.segment_size}"
        f"{'-override' if args.override_segment_size else ''}"
        f"{f'-pitch{args.pitch_shift}' if args.pitch_shift else ''}"
    )
    return RunConfig(
        checkpoint=checkpoint,
        overlap=args.overlap,
        segment_size=args.segment_size,
        override_segment_size=args.override_segment_size,
        pitch_shift=args.pitch_shift,
        config_tag=args.config_tag or default_tag,
    )


def _score_estimate(mixture: Path, truth: Path, estimate: Path) -> dict[str, float]:
    """Shell to scripts/bench/separation_metrics.py so local and any future
    remote scoring agree exactly with the rest of the shootout harness."""
    proc = subprocess.run(
        [
            "uv",
            "run",
            "scripts/bench/separation_metrics.py",
            "--mixture",
            str(mixture),
            "--truth",
            str(truth),
            "--estimates",
            str(estimate),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"separation_metrics.py failed for {estimate}: {proc.stderr}")
    return json.loads(proc.stdout)[str(estimate)]


def _append_ledger(entry: dict[str, Any]) -> None:
    if LEDGER_PATH.is_file():
        ledger = json.loads(LEDGER_PATH.read_text())
    else:
        ledger = {"runs": []}
    ledger["runs"].append(entry)
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2) + "\n")


# ----- CLI: bench (the fixed 6-track MUSDB harness) --------------------------


def cmd_bench(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(BENCH_DIR))
    import shootout_spec  # stdlib-only, safe to import directly (see its docstring)

    config = _resolve_config(args)
    # shootout_spec.TRACKS holds TWELVE tracks; MODEL-SHOOTOUT.md (and this
    # spike's HTDEMUCS_FT_SISDR comparison table) only ever scored the FIRST
    # SIX -- confirmed by title order against the MODEL-SHOOTOUT.md table.
    # Slicing the full 12 here would silently score six extra tracks with no
    # htdemucs_ft number to pair against, and "run all" would stop meaning
    # "run the 6-track baseline". BENCH_TRACKS is that fixed 6-track subset;
    # --limit slices further for a quick smoke test.
    bench_tracks = shootout_spec.TRACKS[: len(HTDEMUCS_FT_SISDR)]
    tracks = bench_tracks[: args.limit] if args.limit else bench_tracks
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[tuple[Any, Path, Path]] = []
    for track in tracks:
        mix_path = CLIPS_MUSDB_DIR / f"{track.slug}-mixture.flac"
        truth_path = CLIPS_MUSDB_DIR / f"{track.slug}-truth-vocals.flac"
        for path in (mix_path, truth_path):
            if not path.is_file():
                raise SystemExit(
                    f"error: harness excerpt missing: {path}. Do not regenerate "
                    "before checking bifrost2:D:/asset-store/shootout (see "
                    "LARGE-ARTIFACTS.md and scripts/bench/CLAUDE.md) -- the "
                    "6-track shootout excerpts are very likely already archived."
                )
        jobs.append((track, mix_path, truth_path))

    print(
        f"[OK] config_tag={config.config_tag} checkpoint={config.checkpoint} "
        f"overlap={config.overlap} segment_size={config.segment_size} "
        f"tracks={len(jobs)}",
        flush=True,
    )

    wall0 = time.perf_counter()
    with app.run():
        remote_results = list(
            separate_track.starmap(
                [
                    (
                        mix_path.read_bytes(),
                        mix_path.name,
                        track.slug,
                        config.checkpoint,
                        config.overlap,
                        config.segment_size,
                        config.override_segment_size,
                        config.pitch_shift,
                        config.config_tag,
                        str(mix_path),
                    )
                    for track, mix_path, _ in jobs
                ]
            )
        )
    wall_s = time.perf_counter() - wall0

    per_track_sisdr: dict[str, float] = {}
    per_track_delta: dict[str, float] = {}
    gpu_s_total = 0.0
    failures: dict[str, str] = {}
    for (track, mix_path, truth_path), result in zip(jobs, remote_results):
        gpu_s_total += result.get("gpu_s", 0.0)
        if "error" in result:
            failures[track.slug] = result["error"]
            print(f"[FAIL] {track.title}: {result['error']}", flush=True)
            continue
        vocals_path = out_dir / f"{track.slug}-{config.config_tag}.flac"
        inst_path = out_dir / f"{track.slug}-{config.config_tag}.inst.flac"
        vocals_path.write_bytes(result["vocals"])
        inst_path.write_bytes(result["instrumental"])
        metrics = _score_estimate(mix_path, truth_path, vocals_path)
        si_sdr = metrics["si_sdr"]
        per_track_sisdr[track.slug] = si_sdr
        if track.slug in HTDEMUCS_FT_SISDR:
            per_track_delta[track.slug] = round(si_sdr - HTDEMUCS_FT_SISDR[track.slug], 3)
        print(
            f"[OK] {track.title}: si_sdr={si_sdr:.2f} dB "
            f"(htdemucs_ft={HTDEMUCS_FT_SISDR.get(track.slug, float('nan')):.2f}, "
            f"delta={per_track_delta.get(track.slug, float('nan')):+.2f}) "
            f"gpu_s={result['gpu_s']:.1f}",
            flush=True,
        )

    if failures:
        raise SystemExit(f"error: {len(failures)}/{len(jobs)} tracks failed: {failures}")

    median_sisdr = round(statistics.median(per_track_sisdr.values()), 3)
    median_delta = (
        round(statistics.median(per_track_delta.values()), 3) if per_track_delta else None
    )
    accepted = median_delta is not None and median_delta >= GOAL_MEDIAN_PAIRED_DELTA_DB

    note = (
        f"MDXC overlap ({config.overlap}) is an integer window-overlap count, "
        "not the demucs 0..1 fraction -- 'overlap 0.25 equivalent' from the "
        "task brief is honoured as this architecture's own library default, "
        "not a numeric conversion. TTA is unsupported for MDXC/Roformer in "
        "audio-separator (VR-arch only), so tta=false is a hard constraint "
        "here, not a choice. 'accepted' applies the plan's GOAL threshold "
        f"(median paired delta >= +{GOAL_MEDIAN_PAIRED_DELTA_DB} dB vs "
        "htdemucs_ft), not the +0.2 dB 'accept a lever change vs the previous "
        "round' rule -- there is no previous roformer round yet."
    )
    if len(jobs) < len(bench_tracks):
        note += f" PARTIAL RUN: {len(jobs)}/{len(bench_tracks)} tracks (smoke test)."

    entry = {
        "config_tag": config.config_tag,
        "per_track_sisdr": per_track_sisdr,
        "median": median_sisdr,
        "wall_s": round(wall_s, 1),
        "gpu_s": round(gpu_s_total, 1),
        "accepted": accepted,
        "note": note,
        # extra fields, additive -- the four required by the task are above
        "checkpoint": config.checkpoint,
        "overlap": config.overlap,
        "segment_size": config.segment_size,
        "override_model_segment_size": config.override_segment_size,
        "pitch_shift": config.pitch_shift,
        "tta": False,
        "gpu_kind": GPU_KIND,
        "track_count": len(jobs),
        "htdemucs_ft_per_track_delta": per_track_delta,
        "htdemucs_ft_median_paired_delta": median_delta,
        "timestamp": datetime.now(UTC).isoformat(),
        "runner_sha256": _runner_sha256(),
    }
    _append_ledger(entry)

    print(
        f"\n[DONE] median_sisdr={median_sisdr} htdemucs_ft_median_paired_delta="
        f"{median_delta} accepted={accepted} wall_s={wall_s:.1f} "
        f"gpu_s={gpu_s_total:.1f}\nledger: {LEDGER_PATH}",
        flush=True,
    )


# ----- CLI: separate (ad-hoc, arbitrary input -- reused by later steps e.g.
#       the plan's library render, out of scope for this baseline run) -------


def cmd_separate(args: argparse.Namespace) -> None:
    config = _resolve_config(args)
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    inputs: list[Path] = args.input
    for path in inputs:
        if not path.is_file():
            raise SystemExit(f"error: input file does not exist: {path}")

    with app.run():
        remote_results = list(
            separate_track.starmap(
                [
                    (
                        path.read_bytes(),
                        path.name,
                        path.stem,
                        config.checkpoint,
                        config.overlap,
                        config.segment_size,
                        config.override_segment_size,
                        config.pitch_shift,
                        config.config_tag,
                        str(path),
                    )
                    for path in inputs
                ]
            )
        )

    for path, result in zip(inputs, remote_results):
        if "error" in result:
            print(f"[FAIL] {path.name}: {result['error']}", flush=True)
            continue
        vocals_path = out_dir / f"{path.stem}-vocals.flac"
        inst_path = out_dir / f"{path.stem}-instrumental.flac"
        vocals_path.write_bytes(result["vocals"])
        inst_path.write_bytes(result["instrumental"])
        (out_dir / f"{path.stem}-meta.json").write_text(
            json.dumps(result["meta"], indent=2) + "\n"
        )
        print(f"[OK] {path.name} -> {vocals_path.name}, {inst_path.name}", flush=True)


# ----- CLI: pull (re-fetch from the Volume without spending GPU time) -------


def cmd_pull(args: argparse.Namespace) -> None:
    out_dir: Path = args.out_dir
    prefix = args.config_tag
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for entry in volume.iterdir(prefix, recursive=True):
        if entry.type != modal.volume.FileEntryType.FILE:
            continue
        rel = Path(entry.path).relative_to(prefix) if prefix else Path(entry.path)
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as handle:
            for chunk in volume.read_file(entry.path):
                handle.write(chunk)
        count += 1
        print(f"[OK] {entry.path} -> {dest}", flush=True)
    if count == 0:
        raise SystemExit(f"error: nothing found under {prefix!r} in volume {VOLUME_NAME!r}")
    print(f"[DONE] pulled {count} files to {out_dir}", flush=True)


# ----- entrypoint -------------------------------------------------------------


def _add_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--checkpoint", default=DEFAULT_CHECKPOINT)
    parser.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP)
    parser.add_argument("--segment-size", type=int, default=DEFAULT_SEGMENT_SIZE)
    parser.add_argument("--override-segment-size", action="store_true")
    parser.add_argument("--pitch-shift", type=int, default=DEFAULT_PITCH_SHIFT)
    parser.add_argument("--tta", action="store_true", help="unsupported; refuses fast")
    parser.add_argument(
        "--config-tag",
        default=None,
        help="defaults to a name derived from checkpoint+overlap+segment_size",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    bench = sub.add_parser("bench", help="run the fixed 6-track MUSDB shootout harness")
    bench.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    bench.add_argument(
        "--limit", type=int, default=0, help="run only the first N tracks (0 = all 6)"
    )
    _add_config_args(bench)
    bench.set_defaults(func=cmd_bench)

    separate = sub.add_parser("separate", help="separate arbitrary local audio files")
    separate.add_argument("--input", type=Path, nargs="+", required=True)
    separate.add_argument("--out-dir", type=Path, required=True)
    _add_config_args(separate)
    separate.set_defaults(func=cmd_separate)

    pull = sub.add_parser("pull", help="pull a config_tag's outputs from the Volume")
    pull.add_argument("--config-tag", required=True)
    pull.add_argument("--out-dir", type=Path, required=True)
    pull.set_defaults(func=cmd_pull)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
