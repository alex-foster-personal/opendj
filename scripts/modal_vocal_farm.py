#!/usr/bin/env python3
"""Modal batch farm: fill the vocal-cache gap on serverless GPUs.

The production runner that supersedes ``scripts/vocal_gcloud_farm.py``
by fanning out over Modal containers with the htdemucs weights baked into
the image. Source audio is uploaded by the local runner.

ONE demucs pass yields BOTH products. demucs computes drums/bass/other/vocals
together whether or not you keep them, so separating now for vocal regions and
again later for stems would pay the same GPU twice.

TRANSFER MUST NEVER BLOCK THE GPU. Moving bytes is neither GPU nor CPU work,
so it belongs on a queue, overlapped with separation - never on the critical
path between one result and the next. Two consequences shape this file:

  * ``--stems-dest r2`` (preferred) uploads the four FLAC stems to Cloudflare
    R2 from INSIDE the container, over the datacenter's uplink. The Mac never
    sees a stem byte, and the function's return value shrinks to the small
    region JSON. This is the only shape that scales: it removes the Mac from
    the artifact path entirely instead of merely making it asynchronous.
  * ``--stems-dest bifrost2`` keeps the old destination but hands each bundle
    to a bounded worker pool (``PublishQueue``), so transfer
    overlaps the next track's separation instead of serialising with it.
    The queue is BOUNDED because the bundles are in RAM; see PublishQueue.

Uploads are pipelined by construction: ``.starmap`` is fed a generator, so a
track starts on the GPU the moment ITS OWN bytes land, not when the batch
finishes reading.

Shape of a run: compute the gap locally -> ``.starmap`` audio bytes at the
chosen preset -> each container separates once, ships its stems to the chosen
destination, and returns region JSON -> the local loop writes that JSON to
``data/state/vocal-cache/<stable_id>.json`` through
``apps.vocals.cache.write_entry`` so the contract stays single-sourced.

Moving stems onward from R2 to bifrost2 is a SEPARATE, resumable job
(``scripts/r2_stem_sync.py``). It is safe to interrupt and safe to re-run, and
it never shares a process with a GPU run.

Every state change is also appended to ``data/state/farm-logs/<run-id>.jsonl``
(``scripts/farm_progress.py``), which ``scripts/farm_dashboard.py`` renders and
an agent can ``tail -f``.

The separation maths (thresholds, hop, hysteresis, confidence gain,
intensity ramp) and the Modal image definition are VENDORED VERBATIM from
``scripts/modal_vocal_spike.py`` -- byte-identical image layers are what
keep the ~80MB weight bake a cache hit instead of a rebuild.

WHY THE REPO IMPORTS ARE FUNCTION-LOCAL: Modal imports THIS MODULE inside
the container to find ``separate_track``. The container has no ``apps``
package, so a top-level ``from apps.vocals import cache`` would fail every
task at import time. Module scope stays stdlib + modal; repo imports live
in the local-only helpers.

Requirements (mini-PRD):
  ✔︎ ✅ gap = playlist_memberships JOIN tracks, minus an existing
    vocal-cache/<stable_id>.json, minus rows whose file_path is not a file.
    [if] a track already has a cache file [then] it is never re-uploaded
    [if] file_path points at a missing file [then] it is excluded, not failed
  ✔︎ ✅ preset is a CLI flag defaulting to hdemucs_mmi at overlap 0.25, which
    remains selectable alongside the other presets. The default is a
    configuration choice, not a guarantee of quality on a particular track.
    Every rung of the old overlap ladder stays selectable.
    [if] --preset htdemucs-ov0.5 [then] overlap 0.5 reaches apply_model
    [if] a preset names a model not baked into the image [then ⛔️] refuse
    unless --allow-unbaked-model, because it silently costs a cold download
  ✔︎ ✅ every entry is stamped with {tag, model, overlap, shifts, rung} so a
    later quality upgrade can re-run selectively.
    [if] an entry is written [then] entry['preset']['tag'] == the run's tag
  ✔︎ ✅ results are written incrementally, so a mid-run abort keeps every
    track completed so far.
    [if] the run is killed after k results [then] k cache files exist
  ✔︎ ✅ per-track failures are collected and never abort the batch.
    [if] a track raises in-container [then] it returns {error}, batch continues
    [if] every track fails [then] exit code 1 and the failure list is written
  ✔︎ ✅ drift guard: region seconds live in the SOURCE timeline; a resampled
    duration that does not match ffprobe's refuses to emit.
  ✔︎ ✅ one pass emits both products: the same apply_model call feeds the vocal
    regions and the four FLAC stems.
    [if] --no-stems [then] regions only, and the GPU cost is unchanged
    [if] a stem's frame count or channel count differs from vocals' [then ⛔️]
  ✔︎ ✅ a bundle is deleted locally ONLY after bifrost2 reports byte-identical
    sizes for all five files.
    [if] any remote size differs [then ⛔️] raise, keep nothing, report the track
    [if] the tailnet is down [then ⛔️] fail before any GPU time is spent

Run (modal is not a repo dependency -- overlay it):
  uv run --with modal python scripts/modal_vocal_farm.py --dry-run
  uv run --with modal python scripts/modal_vocal_farm.py --limit 10
  uv run --with modal python scripts/modal_vocal_farm.py

-Claude
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import modal

# ----- calibrated region params (VENDORED from scripts/modal_vocal_spike.py,
#       itself vendored from scripts/vocal_region_worker.py -- do NOT tune) ----
HOP_S: float = 0.5  # RMS envelope hop
ON_RATIO: float = 0.10  # vocals-rms / mix-rms to enter a region
OFF_RATIO: float = 0.05  # ...to exit (hysteresis)
MERGE_GAP_S: float = 1.5  # merge regions separated by < this gap
MIN_REGION_S: float = 1.0  # drop merged regions shorter than this
CONFIDENCE_GAIN: float = 2.5  # confidence = min(1, GAIN * max ratio in region)
DRIFT_ABS_TOL_S: float = 0.5  # resampled duration must match source within...
DRIFT_REL_TOL: float = 0.005  # ...max(abs, rel * source) seconds

# The cache contract constants, duplicated here ONLY because the container
# cannot import apps.vocals.cache. _assert_contract_matches() below fails the
# run locally if these ever drift from the real module.
CACHE_SCHEMA: int = 2
# READ THIS BEFORE TRUSTING THE STRING. "demucs-htdemucs" is a FAMILY and
# SCHEMA marker, not a record of which model ran. It predates running anything
# but htdemucs and cannot be changed per model: apps/vocals/cache.py raises on
# any other value at both read and write, so emitting "demucs-hdemucs_mmi"
# would make every new entry unreadable by the webui until that shared contract
# widened, and would split the library into two populations meanwhile.
# The model that ACTUALLY ran is recorded twice per entry and both are honest:
# entry["preset"]["model"] and entry["params"]["model"].
#
# DECIDED Fri 24 Jul 2026, do not re-litigate without new information: widening
# apps/vocals/cache.py to accept a set of sources was considered and REJECTED.
# It is blast radius on a shared contract in exchange for a nicer label, and it
# would split the library into two populations mid-flight, which is exactly
# what the re-farm of the existing entries exists to avoid.
CACHE_SOURCE: str = "demucs-htdemucs"

# Models whose weights are baked into the image at build time. Anything NOT in
# here costs a cold download PER CONTAINER on first use, which is why
# _resolve_preset refuses an unbaked model unless you say so explicitly.
# htdemucs_ft is deliberately absent: it is a per-source FINE-TUNED bag of four
# separate models, so baking it multiplies the image weight for a preset we run
# only when someone specifically wants the top of the quality board.
# htdemucs_ft IS now baked, despite being four separate per-source models and
# multiplying the image weight. It is the L tier (apps/stems/tiers.py), and a
# tier nobody can run without a per-container cold download is a tier that never
# gets run. Baking it is a one-off build cost against an unbounded runtime one.
BAKED_MODELS: tuple[str, ...] = ("htdemucs", "hdemucs_mmi", "htdemucs_ft")
MODEL_NAME: str = "htdemucs"  # legacy alias, still the schema/source family
DEMUCS_VERSION: str = "4.0.1"  # matches the pinned wheel in the image below
STEM_PARTS: tuple[str, str, str, str] = ("vocals", "drums", "bass", "other")
STEM_BUNDLE_SCHEMA: int = 2  # v1 is WAV-only; see _stem_manifest for the delta

# WHICH CARD. Resolved at IMPORT, because @app.function's gpu= is evaluated when
# the module is imported, not when a call is dispatched -- so it cannot be a
# plain CLI argument. main() sets this env var from --gpu before the decorated
# function is hydrated, and the container inherits the same value.
#
# The default moved L4 -> H100 on Fri 24 Jul 2026. H100 is the fastest card the
# PINNED STACK can use, not the fastest card Modal sells: torch 2.5.1/cu124
# predates Blackwell (sm_100), so a B200/B300 container fails to launch a kernel
# rather than running slowly. See apps/stems/tiers.py for the full rationale and
# for what a Blackwell migration would cost.
GPU_ENV_VAR: str = "MDT_FARM_GPU"
DEFAULT_GPU_KIND: str = "H100"
GPU_KIND: str = os.environ.get(GPU_ENV_VAR, DEFAULT_GPU_KIND)
TORCH_CACHE_DIR: str = "/root/.cache/torch"
TASK_TIMEOUT_S: int = 900
DEFAULT_MAX_CONTAINERS: int = 10  # Modal's day-1 concurrent-GPU allowance
INFRA_RETRIES: int = 1  # in-container errors return {error} and never retry
# Read-ahead for the input feeder. Modal's sync .starmap pulls its input
# iterator INLINE on the event-loop thread, so a blocking read there stalls
# uploads, dispatch and output collection alike (see _inputs).
#
# These MIRROR apps.vocals.prefetch's measured defaults and are duplicated here
# only because Modal imports this module inside the container, which has no
# ``apps`` package, so they cannot be imported at module scope. A test asserts
# the two stay equal. Override per run with --prefetch-workers / --prefetch-depth;
# depth 1 with 1 worker is the SERIAL control arm.
PREFETCH_DEPTH: int = 32
PREFETCH_WORKERS: int = 16
PREFETCH_MAX_BYTES: int = 512_000_000
# Modal's published per-second rates (https://modal.com/pricing, Fri 24 Jul
# 2026), MIRRORED from apps/stems/tiers.py because the container cannot import
# apps. _assert_contract_matches() fails the run locally if the two drift.
GPU_USD_PER_S: dict[str, float] = {
    "H100": 0.001097,
    "H200": 0.001261,
    "A100-80GB": 0.000694,
    "L40S": 0.000542,
    "L4": 0.000222,
}


def _gpu_usd_per_s() -> float:
    if GPU_KIND not in GPU_USD_PER_S:
        raise SystemExit(
            f"error: no published rate for {GPU_KIND!r}; "
            f"known: {', '.join(sorted(GPU_USD_PER_S))}"
        )
    return GPU_USD_PER_S[GPU_KIND]


@dataclass(frozen=True)
class Preset:
    """One rung of the measured quality ladder (HANDOFF section 2)."""

    tag: str
    model: str
    overlap: float
    shifts: int
    rung: int

    def stamp(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "model": self.model,
            "overlap": self.overlap,
            "shifts": self.shifts,
            "rung": self.rung,
        }


# Preset tags preserve historical rung identifiers for cache provenance;
# the separately configured default is selected by DEFAULT_PRESET below.
PRESETS: dict[str, Preset] = {
    # rung 0 = NOT a rung of the overlap ladder. hdemucs_mmi was never in
    # scripts/bench/ladder.json, so giving it a ladder position would invent a
    # measurement. It is a separately selectable model configuration.
    "hdemucs_mmi-ov0.25": Preset("hdemucs_mmi-ov0.25", "hdemucs_mmi", 0.25, 0, 0),
    "hdemucs_mmi-ov0.1": Preset("hdemucs_mmi-ov0.1", "hdemucs_mmi", 0.1, 0, 0),
    "htdemucs-ov0.1": Preset("htdemucs-ov0.1", "htdemucs", 0.1, 0, 3),
    "htdemucs-ov0.25": Preset("htdemucs-ov0.25", "htdemucs", 0.25, 0, 4),
    "htdemucs-ov0.5": Preset("htdemucs-ov0.5", "htdemucs", 0.5, 0, 5),
    "htdemucs_ft-ov0.25": Preset("htdemucs_ft-ov0.25", "htdemucs_ft", 0.25, 0, 7),
    "htdemucs_ft-ov0.5": Preset("htdemucs_ft-ov0.5", "htdemucs_ft", 0.5, 0, 8),
}
# The default selects an existing preset tag. Other presets remain explicit
# choices; no track-specific listening or quality guarantee is implied.
DEFAULT_PRESET: str = "hdemucs_mmi-ov0.25"

# Where a run's stems go. "r2" is the only one that keeps the Mac off the
# artifact path; "bifrost2" is the legacy direct-scp destination token.
# It requires an operator-configured SSH alias or MDT_B2STORE_SSH_HOST.
# "local" writes straight into data/state/stems/<stable_id>/, which is what the
# webui mute/solo path reads -- so a farmed bundle is auditionable in the app
# immediately. It is the ONLY dest that leaves bytes on this Mac on purpose, so
# choose it only when local storage capacity is sufficient for the run.
STEM_DESTS: tuple[str, ...] = ("r2", "bifrost2", "local", "none")
DEFAULT_STEM_DEST: str = "bifrost2"

# R2 credentials, named to match apps/cloud/config.py so the repo has ONE set
# of R2 env var names rather than a farm-private set. Supplied by
# `doppler run --` locally and forwarded to the container as an ephemeral
# Modal secret; they are never arguments to a Modal call, so they cannot land
# in a call payload or a task log.
R2_ENV_KEYS: tuple[str, str, str] = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)
DEFAULT_R2_BUCKET: str = "music-dj-audio"  # matches MUSIC_DJ_AUDIO_BUCKET

#: Pre-unification path-addressed stems layout (cloudsync issue #1452).
#: Retained only so legacy objects are recognizable; the R2 destination now
#: writes content-addressed keys below.
R2_STEM_PREFIX: str = "stems"  # -> stems/<preset-tag>/<stable_id>/<part>.flac (legacy)

#: The one content-addressed R2 layout (mirrored from apps/cloud/r2_keys.py;
#: tests/scripts/test_stem_r2_parity.py pins this copy to the app layer so it
#: cannot drift). Every object this farm puts to R2 is keyed by its own body.
R2_CONTENT_ADDRESSED_KEY: str = "assets/{sha256[:2]}/{sha256}"
ASSET_PREFIX: str = "assets"
HASH_SHARD_LEN: int = 2
R2_MAX_ATTEMPTS: int = 3

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
FAILURES_DIR: Path = REPO_ROOT / ".tmp/bench/modal_farm"
# Staged OUTSIDE data/state/stems so a bundle in flight can never be confused
# with, or clean up, an existing local stem bundle.
STEMS_OUTBOX: Path = REPO_ROOT / ".tmp/stems-outbox"
STEMS_REMOTE_ROOT: str = "stems"  # -> D:/asset-store/stems/<preset-tag>/<stable_id>
# Four 16-bit 44.1k stereo stems at ~42% FLAC compression, against a ~320kbps
# mp3 source. The existing conservative factor guards local storage before
# a run; actual bundle size depends on source audio and codec.
STEM_SIZE_FACTOR: int = 8
SLACK_BYTES: int = 2_000_000_000


# ----- pure region maths (VENDORED VERBATIM from the spike; torch-free) -------


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
    """PoC confidence: clipped CONFIDENCE_GAIN x max ratio inside the region."""
    i0 = int(start_s / hop_s)
    i1 = max(i0 + 1, int(end_s / hop_s))
    values = [v for t, v in ratio_env[i0:i1]]
    if not values:
        raise ValueError(f"region [{start_s}, {end_s}] has no envelope frames")
    return round(min(1.0, CONFIDENCE_GAIN * max(values)), 2)


def intensity_of(confidence: float) -> int:
    """Map worker confidence (0..1) onto the PVDI intensity ramp (1..4)."""
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence out of range [0, 1]: {confidence}")
    return max(1, min(4, round(confidence * 4)))


def regions_from_envelope(
    ratio_env: list[tuple[float, float]], duration_s: float, hop_s: float = HOP_S
) -> tuple[list[dict[str, Any]], float]:
    """Cache-shaped regions ({start_s, end_s, confidence, intensity}) + coverage%."""
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


def region_params(preset: Preset) -> dict[str, Any]:
    """The ``params`` block stored on every cache entry: the region maths the
    regions were cut with, plus the separation config that produced them."""
    return {
        "model": preset.model,
        "hop_s": HOP_S,
        "on_ratio": ON_RATIO,
        "off_ratio": OFF_RATIO,
        "merge_gap_s": MERGE_GAP_S,
        "min_region_s": MIN_REGION_S,
        "confidence_gain": CONFIDENCE_GAIN,
        "overlap": preset.overlap,
        "shifts": preset.shifts,
        "preset": preset.tag,
    }


# ----- Modal image (VENDORED VERBATIM from the spike: identical layers are
#       what make the weight bake a cache hit rather than a 3-minute rebuild) --


def _download_weights() -> None:
    """Build step: fetch every BAKED_MODELS bag into the image filesystem so a
    cold container does not re-fetch it.

    CHANGING THIS FUNCTION REBUILDS THE IMAGE ONCE. Modal keys the layer on the
    function's source, and the vendored-verbatim rule above exists precisely to
    keep that a cache hit. Adding hdemucs_mmi is a deliberate one-off rebuild,
    not an accident.
    """
    from demucs.pretrained import get_model

    for name in BAKED_MODELS:
        model = get_model(name)
        model.eval()
        print(f"[build] baked {name} weights into {TORCH_CACHE_DIR}")


image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install(
        "demucs==4.0.1",
        "torch==2.5.1",
        "torchaudio==2.5.1",
        "soundfile>=0.12",
        "numpy<2",
        # boto3 is appended AFTER the pinned ML stack so it cannot perturb
        # their resolution, and the layer above it (the weight bake) stays a
        # cache hit. It is the S3 client for the in-container R2 upload.
        "boto3>=1.34",
    )
    .run_function(_download_weights)
)

app = modal.App(name="mdt-vocal-farm", image=image)


def _r2_secrets() -> list[modal.Secret]:
    """R2 credentials for the container, read from THIS process's environment.

    Built as an ephemeral ``Secret.from_dict`` rather than a pre-registered
    named secret so the only place the values live is Doppler: run the farm
    under ``doppler run --`` and they flow through; run it without and the
    list is empty, which makes a ``--stems-dest r2`` run fail loudly in the
    container rather than silently writing nowhere.

    Returns an empty list when the keys are absent. That is also what happens
    when the CONTAINER imports this module (its environment has no R2 keys),
    which is harmless: the decorator's secret list is resolved locally, at
    hydration, not container-side.
    """
    import os

    if not all(os.environ.get(key) for key in R2_ENV_KEYS):
        return []
    return [modal.Secret.from_dict({key: os.environ[key] for key in R2_ENV_KEYS})]


# ----- remote GPU function ----------------------------------------------------


def _rms_envelope(wav: Any, sr: int, hop_s: float) -> list[float]:
    mono = wav.mean(dim=0)
    hop = int(sr * hop_s)
    n = len(mono) // hop
    return [float(mono[i * hop : (i + 1) * hop].pow(2).mean().sqrt()) for i in range(n)]


def _envelope_payload(vocals_rms: list[float], mix_rms: list[float]) -> dict[str, Any]:
    """The per-hop RMS pair as float16 bytes, narrowed HERE not on the Mac.

    Two reasons the narrowing happens in the container: it has numpy for
    certain, and float16 halves what crosses the wire home. v and m are kept
    SEPARATE because neither survives being folded into their ratio, and an
    absolute-level analysis needs the absolute values. See apps/vocals/envelope
    for why float16 is safe against the 0.10 / 0.05 thresholds.
    """
    import numpy as np

    if len(vocals_rms) != len(mix_rms):
        raise RuntimeError(
            f"envelope length mismatch: vocals {len(vocals_rms)} != mix {len(mix_rms)}"
        )
    return {
        "hop_s": HOP_S,
        "frames": len(vocals_rms),
        "dtype": "float16",
        "vocals_rms": np.asarray(vocals_rms, dtype="<f2").tobytes(),
        "mix_rms": np.asarray(mix_rms, dtype="<f2").tobytes(),
    }


def _separate_all(
    model: Any, wav: Any, device: str, overlap: float, shifts: int
) -> dict[str, Any]:
    """All four demucs sources from ONE pass.

    demucs computes drums/bass/other/vocals together whether or not you keep
    them, so pulling only the vocals and separating again later for stems pays
    the same GPU twice. The reference normalisation is verbatim from
    scripts/stem_bundle_worker.py::_separate_all so archived bundles match the
    ones that worker already wrote.
    """
    from demucs.apply import apply_model

    ref = wav.mean(0)
    normed = (wav - ref.mean()) / ref.std()
    sources = apply_model(
        model,
        normed[None],
        device=device,
        overlap=overlap,
        shifts=shifts,
        progress=False,
    )[0]
    return {
        name: sources[model.sources.index(name)] * ref.std() + ref.mean()
        for name in STEM_PARTS
    }


# STEM CODEC. Stems are several times larger than their source, so the wire
# cost is worth choosing deliberately rather than inheriting.
#
# Count tracks whose source audio is actually available before estimating
# storage, transfer or separation costs. Direct-scp destinations transfer stem
# bytes through the local runner; r2 uploads from the remote container.
# Estimates depend on actual source duration, codec and transfer capacity.
#
# WHY LOSSY IS DEFENSIBLE FOR THIS PRODUCT, and where it is not: these stems
# drive deck mute/solo and live mashups, not mastering. The separation itself
# is vastly more destructive than any of these codecs, and most sources are
# already ~320 kbps MP3. Keep FLAC for the L rung and for anything that will be
# SCORED -- SI-SDR against a lossy stem measures the codec as well as the model.
STEM_CODECS: dict[str, dict[str, Any]] = {
    # ext, ffmpeg args, and existing approximate per-track storage estimates
    "flac": {"ext": "flac", "args": None, "mb_per_track": 117},
    # AAC is omitted: MP4 requires seeking for its container metadata and
    # ADTS does not provide the duration guarantees required for aligned stems.
    "opus": {
        "ext": "opus",
        # Default args are a fallback only. Prefer opus_ffmpeg_audio_args(source_kbps)
        # so bitrate follows the input (192k-per-stem was ~4x a 320k MP3).
        "args": ["-c:a", "libopus", "-b:a", "80k"],
        "mb_per_track": 12,  # ~80k x4 on a ~4 min track; was 27 at fixed 192k
    },
}
DEFAULT_STEM_CODEC: str = "flac"

# MIRRORED from apps/stems/stem_size_policy.py -- the GPU container has no
# ``apps`` package. _assert_stem_size_policy_mirror_matches() fails locally if
# the two drift.
_STEM_PART_COUNT: int = 4
_OPUS_MIN_KBPS: int = 48
_OPUS_MAX_KBPS: int = 128
_LOSSY_SIZE_GATED: frozenset[str] = frozenset({"opus", "mp3"})  # mp3 added for
# the roformer spike's lossy-source policy (apps/stems/stem_size_policy.py);
# this farm still only ever encodes opus, but the mirror-equality check below
# demands the full set match the source of truth.
_CONTROL_CODECS: frozenset[str] = frozenset({"flac", "wav"})


def _effective_bitrate_kbps(size_bytes: int, duration_s: float) -> float:
    if size_bytes <= 0:
        raise ValueError(f"size_bytes must be positive, got {size_bytes}")
    if duration_s <= 0:
        raise ValueError(f"duration_s must be positive, got {duration_s}")
    return size_bytes * 8 / duration_s / 1000


def _opus_kbps_per_stem(source_kbps: float) -> int:
    if source_kbps <= 0:
        raise ValueError(f"source_kbps must be positive, got {source_kbps}")
    raw = round(source_kbps / _STEM_PART_COUNT)
    return max(_OPUS_MIN_KBPS, min(_OPUS_MAX_KBPS, raw))


def _opus_ffmpeg_audio_args(source_kbps: float) -> list[str]:
    kbps = _opus_kbps_per_stem(source_kbps)
    return ["-c:a", "libopus", "-b:a", f"{kbps}k"]


def _assert_lossy_stems_not_larger_than_source(
    *,
    source_bytes: int,
    part_sizes: dict[str, int],
    codec: str,
) -> None:
    if codec in _CONTROL_CODECS or codec not in _LOSSY_SIZE_GATED:
        return
    if source_bytes <= 0:
        raise RuntimeError(f"source_bytes must be positive, got {source_bytes}")
    offenders = [f"{n}={s}B" for n, s in part_sizes.items() if s > source_bytes]
    if offenders:
        raise RuntimeError(
            f"lossy stem part(s) exceed source ({source_bytes}B) under codec "
            f"{codec!r}: {', '.join(offenders)}. Encode bitrate must follow "
            f"source (see _opus_kbps_per_stem)."
        )


def _encode_stem(
    tensor: Any,
    sample_rate: int,
    codec: str,
    *,
    source_kbps: float | None = None,
) -> bytes:
    """One stem as bytes in ``codec``. FLAC goes direct; lossy goes via ffmpeg.

    ffmpeg is already in the image (it is there for decode), so this adds no
    layer. Encoding happens IN THE CONTAINER, before the bytes ever leave, so
    the saving applies to the wire and not just to disk at the far end.

    When ``source_kbps`` is set, Opus bitrate is derived so four stems share
    roughly the source's total bitrate (mirrored stem_size_policy).
    """
    if codec == "flac":
        return _encode_flac(tensor, sample_rate)
    spec = STEM_CODECS.get(codec)
    if spec is None:
        raise RuntimeError(
            f"unknown stem codec {codec!r}; known: {', '.join(sorted(STEM_CODECS))}"
        )
    import subprocess

    if codec == "opus" and source_kbps is not None:
        encode_args = _opus_ffmpeg_audio_args(source_kbps)
    else:
        encode_args = list(spec["args"])

    wav = _encode_flac(tensor, sample_rate)  # lossless intermediate, in memory
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            *encode_args,
            *([] if "-f" in encode_args else ["-f", spec["ext"]]),
            "pipe:1",
        ],
        input=wav,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(
            f"ffmpeg {codec} encode failed rc={proc.returncode}: "
            f"{proc.stderr.decode(errors='replace')[-500:]}"
        )
    return proc.stdout


def _encode_flac(tensor: Any, sample_rate: int) -> bytes:
    """One stem as 16-bit FLAC bytes.

    PCM_16 matches what scripts/stem_bundle_worker.py already writes, so FLAC
    is a container/compression change at the same precision. Measured on the
    30 existing WAV bundles, FLAC lands at 42% of WAV (bass ~25%, drums ~50%).

    Not bit-identical to the WAV path, and it cannot be: libsndfile rounds
    float32 to int16 slightly differently per writer, so about half the
    samples differ by at most 1 LSB (-90 dBFS, under the 16-bit noise floor).
    Encoding int16 to FLAC IS lossless; only the float32 entry point rounds.
    """
    import io

    import numpy as np
    import soundfile as sf

    arr = tensor.detach().cpu().numpy().T.astype(np.float32, copy=False)
    if arr.ndim != 2 or arr.shape[1] not in (1, 2):
        raise RuntimeError(f"unexpected stem shape {arr.shape}")
    buffer = io.BytesIO()
    sf.write(buffer, arr, sample_rate, format="FLAC", subtype="PCM_16")
    return buffer.getvalue()


def _r2_client() -> Any:
    """boto3 S3 client pointed at R2, from the container's injected secret.

    Fails loudly and specifically: a missing key here means the run was
    started without ``doppler run --``, and the useful thing to say is which
    key was missing, not ``NoCredentialsError`` from three frames deeper.
    """
    import os

    import boto3
    from botocore.config import Config

    missing = [key for key in R2_ENV_KEYS if not os.environ.get(key)]
    if missing:
        raise RuntimeError(
            f"R2 credentials absent in the container: {missing}. Start the farm "
            "under `doppler run --project general --config dev_personal --` so "
            "_r2_secrets() can forward them."
        )
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(retries={"max_attempts": R2_MAX_ATTEMPTS, "mode": "standard"}),
    )


def _upload_stems_r2(
    stems: dict[str, bytes],
    manifest: dict[str, Any],
    bucket: str,
    preset_tag: str,
    stable_id: str,
) -> dict[str, Any]:
    """Push one bundle to R2 from inside the container. Returns upload stats.

    This is the whole point of the R2 destination: ~74 MB of FLAC leaves over
    the datacenter uplink and never traverses the Mac's connection, so the
    only thing the return value carries home is a few hundred bytes of JSON.

    Keys are CONTENT-ADDRESSED (cloudsync issue #1452): each object is put
    under ``assets/<sha256[:2]>/<sha256>`` where ``sha256`` is the digest of
    that object's own body. There is no ``stems/<preset>/<stable_id>/...``
    path segment any more -- a bundle's identity lives in the manifest body
    (stable_id + preset + per-part digests), and the R2 key is just the
    bytes. What is traded away is browsing a bucket prefix to find a preset's
    output; the sidecar row the host writes from the returned keys is the
    index.

    Verification is the ContentLength R2 reports back on head_object, not a
    re-download: a size mismatch is the failure mode a truncated PUT actually
    produces, and re-reading 74 MB to prove a write would reintroduce the
    transfer cost this destination exists to avoid.
    """
    import hashlib
    import json as _json

    def _asset_key(payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        return f"{ASSET_PREFIX}/{digest[:HASH_SHARD_LEN]}/{digest}"

    client = _r2_client()
    started = time.perf_counter()
    # Keyed by OBJECT KEY, not by byte size. Content addressing means two
    # parts with the same length but different bytes are two different keys;
    # a size-keyed dict would let the second clobber the first, so one object
    # never gets HEAD-verified and vanishes from the returned keys/bytes.
    # Identical bytes collapse to one key on their own, so no dedupe needed.
    written: dict[str, int] = {}
    part_keys: dict[str, str] = {}
    part_digests: dict[str, str] = {}
    for part, payload in stems.items():
        key = _asset_key(payload)
        client.put_object(Bucket=bucket, Key=key, Body=payload)
        written[key] = len(payload)
        part_keys[part] = key
        part_digests[part] = hashlib.sha256(payload).hexdigest()

    manifest_bytes = (_json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    manifest_key = _asset_key(manifest_bytes)
    client.put_object(Bucket=bucket, Key=manifest_key, Body=manifest_bytes)
    written[manifest_key] = len(manifest_bytes)

    for key, size in written.items():
        remote = client.head_object(Bucket=bucket, Key=key)["ContentLength"]
        if remote != size:
            raise RuntimeError(
                f"r2 short write: {bucket}/{key} is {remote} bytes, sent {size}"
            )
    return {
        "dest": "r2",
        "bucket": bucket,
        "layout": R2_CONTENT_ADDRESSED_KEY,
        "bundle": {"preset": preset_tag, "stable_id": stable_id},
        "part_keys": part_keys,
        "part_digests": part_digests,
        "manifest_key": manifest_key,
        "bytes": sum(written.values()),
        "upload_s": round(time.perf_counter() - started, 2),
        "keys": sorted(written),
    }


def _analyse(
    audio_bytes: bytes,
    name: str,
    model_name: str,
    overlap: float,
    shifts: int,
    want_stems: bool,
    stem_codec: str = DEFAULT_STEM_CODEC,
) -> dict[str, Any]:
    """The real work. Raises on anything unexpected; the caller converts a
    raise into a per-track {error} so one bad file cannot abort a batch."""
    import hashlib
    import tempfile

    import torch
    from demucs.audio import AudioFile
    from demucs.pretrained import get_model

    if not torch.cuda.is_available():
        raise RuntimeError("cuda unavailable inside the L4 container")
    device = "cuda"

    load0 = time.perf_counter()
    model = get_model(model_name)
    model.eval()
    load_s = time.perf_counter() - load0

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

    # Drift guard: region seconds are computed on the resampled tensor; that is
    # only the source timeline if the duration survived resampling.
    resampled_duration_s = wav.shape[-1] / model.samplerate
    tolerance = max(DRIFT_ABS_TOL_S, DRIFT_REL_TOL * source_duration_s)
    if abs(resampled_duration_s - source_duration_s) > tolerance:
        raise RuntimeError(
            f"sample-rate drift for {name}: source {source_duration_s:.2f}s @ "
            f"{source_sr} Hz but resampled tensor is {resampled_duration_s:.2f}s "
            f"@ {model.samplerate} Hz (tolerance {tolerance:.2f}s)."
        )

    sep0 = time.perf_counter()
    parts = _separate_all(model, wav, device, overlap, shifts)
    separate_s = time.perf_counter() - sep0

    sr = model.samplerate
    vocals = parts["vocals"]
    vocals_rms = _rms_envelope(vocals, sr, HOP_S)
    mix_rms = _rms_envelope(wav, sr, HOP_S)
    ratio = ratio_envelope(vocals_rms, mix_rms, HOP_S)
    regions, coverage = regions_from_envelope(ratio, source_duration_s)

    result: dict[str, Any] = {
        "schema": CACHE_SCHEMA,
        "source": CACHE_SOURCE,
        "fps": 1.0 / HOP_S,
        "duration_s": round(source_duration_s, 2),
        "coverage_pct": coverage,
        "regions": regions,
        "device": device,
        "timings": {"load_s": round(load_s, 1), "separate_s": round(separate_s, 1)},
        "source_sample_rate": source_sr,
        "analysis_sample_rate": sr,
        # The INPUT the regions above were derived from. Returned so the whole
        # threshold family stays re-derivable locally; see apps/vocals/envelope.
        "envelope": _envelope_payload(vocals_rms, mix_rms),
    }
    if not want_stems:
        return result

    frame_count = int(vocals.shape[-1])
    for part, tensor in parts.items():
        if int(tensor.shape[-1]) != frame_count:
            raise RuntimeError(
                f"stem frame mismatch: {part}={tensor.shape[-1]} vocals={frame_count}"
            )
        if int(tensor.shape[0]) != int(model.audio_channels):
            raise RuntimeError(
                f"stem channel mismatch: {part}={tensor.shape[0]} "
                f"expected={model.audio_channels}"
            )
    encode0 = time.perf_counter()
    source_kbps = _effective_bitrate_kbps(len(audio_bytes), source_duration_s)
    result["stems"] = {
        part: _encode_stem(parts[part], sr, stem_codec, source_kbps=source_kbps)
        for part in STEM_PARTS
    }
    _assert_lossy_stems_not_larger_than_source(
        source_bytes=len(audio_bytes),
        part_sizes={p: len(b) for p, b in result["stems"].items()},
        codec=stem_codec,
    )
    result["stem_codec"] = stem_codec
    result["stem_ext"] = STEM_CODECS[stem_codec]["ext"]
    result["stem_opus_kbps"] = (
        _opus_kbps_per_stem(source_kbps) if stem_codec == "opus" else None
    )
    result["source_kbps"] = round(source_kbps, 1)
    result["timings"]["encode_s"] = round(time.perf_counter() - encode0, 1)
    result["source_sha256"] = hashlib.sha256(audio_bytes).hexdigest()
    result["audio"] = {
        "sample_rate": sr,
        "channels": int(model.audio_channels),
        "frame_count": frame_count,
        "bit_depth": 16,
        "codec": "flac",
    }
    return result


@app.function(
    gpu=GPU_KIND,
    timeout=TASK_TIMEOUT_S,
    max_containers=DEFAULT_MAX_CONTAINERS,
    retries=INFRA_RETRIES,
    secrets=_r2_secrets(),
)
def separate_track(
    audio_bytes: bytes,
    stable_id: str,
    name: str,
    model_name: str,
    overlap: float,
    shifts: int,
    stems_dest: str,
    source_path: str,
    preset_stamp: dict[str, Any],
    r2_bucket: str,
    stem_codec: str = DEFAULT_STEM_CODEC,
) -> dict[str, Any]:
    """One track: separate on an L4, then dispose of the stems per ``stems_dest``.

    ``stems_dest`` decides what crosses the wire home:
      * ``r2``      -> stems uploaded here, return value is JSON only
      * ``bifrost2``-> stems returned as bytes for the local queue to push
      * ``none``    -> no stems at all

    Returns ``{stable_id, error}`` instead of raising, so a corrupt file
    costs one track, not the run. An R2 upload failure is also a per-track
    error rather than a reason to discard completed results.
    """
    entered = time.perf_counter()
    # Wall-clock, deliberately NOT perf_counter: perf_counter's origin is
    # per-process, so two containers' perf_counters cannot be compared. Only an
    # absolute epoch lets the caller reconstruct which calls OVERLAPPED, which
    # is the difference between measuring fan-out and asserting it.
    entered_wall = time.time()
    try:
        result = _analyse(
            audio_bytes,
            name,
            model_name,
            overlap,
            shifts,
            stems_dest != "none",
            stem_codec,
        )
        if stems_dest == "r2":
            # Pop before returning: leaving the bytes in would send ~74 MB per
            # track back through Modal to the Mac, which is the exact cost
            # this destination exists to avoid.
            stems = result.pop("stems")
            manifest = _stem_manifest(stable_id, source_path, result, preset_stamp)
            result["stem_upload"] = _upload_stems_r2(
                stems,
                manifest,
                r2_bucket,
                preset_stamp["tag"],
                stable_id,
            )
    except Exception as exc:  # per-track isolation is the whole point
        return {
            "stable_id": stable_id,
            "error": f"{type(exc).__name__}: {exc}",
            "container_s": round(time.perf_counter() - entered, 1),
            "wall_span": [entered_wall, time.time()],
        }
    result["stable_id"] = stable_id
    # Billable-time proxy: everything this call held the GPU for, not just the
    # separate. Modal also bills the scaledown window, so a total built from
    # this is a lower bound on the invoice, not the invoice.
    result["container_s"] = round(time.perf_counter() - entered, 1)
    # The raw material for OBSERVED concurrency. A failed call gets one too:
    # it still occupied a container, so excluding it would flatter the number.
    result["wall_span"] = [entered_wall, time.time()]
    return result


# ----- local: gap computation -------------------------------------------------


@dataclass(frozen=True)
class GapTrack:
    stable_id: str
    audio_path: Path
    # Both are scheduling keys for sort_longest_first, not descriptive extras.
    # 0 means state.db had no duration for the row; size then carries the sort.
    duration_ms: int = 0
    size_bytes: int = 0


def _assert_contract_matches() -> None:
    """The remote hardcodes the cache schema/source because it cannot import
    apps.vocals.cache. Fail the run locally the moment those drift."""
    from apps.vocals import cache as vcache

    if vcache.VOCAL_CACHE_SCHEMA != CACHE_SCHEMA:
        raise SystemExit(
            f"error: cache schema drifted -- apps.vocals.cache says "
            f"{vcache.VOCAL_CACHE_SCHEMA}, this script emits {CACHE_SCHEMA}. "
            "Update CACHE_SCHEMA and re-check the worker payload."
        )
    if vcache.VOCAL_CACHE_SOURCE != CACHE_SOURCE:
        raise SystemExit(
            f"error: cache source drifted -- apps.vocals.cache says "
            f"{vcache.VOCAL_CACHE_SOURCE!r}, this script emits {CACHE_SOURCE!r}."
        )
    probe = Path(__file__)
    if _signature_of(probe.stat()) != vcache.audio_signature(probe):
        raise SystemExit(
            "error: audio_signature shape drifted -- _signature_of no longer "
            "reproduces apps.vocals.cache.audio_signature, so every entry this "
            "farm writes would self-invalidate at read time."
        )
    _assert_tier_mirror_matches()


def _assert_tier_mirror_matches() -> None:
    """apps/stems/tiers.py is the source of truth; this module mirrors it.

    The mirror exists because Modal imports THIS file inside the container,
    where there is no ``apps`` package. Mirrors rot silently, so check it on
    every run: a tier naming a preset this farm cannot run, or a price table
    that has drifted, both fail here rather than after paying for a batch.
    """
    from apps.stems import tiers

    # LOCAL rungs never touch this farm, so their preset is not one of ours.
    for tier in tiers.modal_tiers():
        if tier.preset_tag not in PRESETS:
            raise SystemExit(
                f"error: tier {tier.key} names preset {tier.preset_tag!r}, "
                f"which this farm cannot run. Known: {', '.join(sorted(PRESETS))}"
            )
        rung = PRESETS[tier.preset_tag]
        if (rung.model, rung.overlap, rung.shifts) != (
            tier.model,
            tier.overlap,
            tier.shifts,
        ):
            raise SystemExit(
                f"error: tier {tier.key} and preset {tier.preset_tag!r} "
                f"disagree -- tier says {tier.model}/{tier.overlap}/"
                f"{tier.shifts}, preset says {rung.model}/{rung.overlap}/"
                f"{rung.shifts}."
            )
        if tier.model not in BAKED_MODELS:
            raise SystemExit(
                f"error: tier {tier.key} needs model {tier.model!r}, which is "
                f"not baked into the image (baked: {', '.join(BAKED_MODELS)}). "
                "A tier that costs a cold weight download per container is a "
                "tier nobody will run."
            )
    if tiers.GPU_USD_PER_S != GPU_USD_PER_S:
        raise SystemExit(
            "error: GPU price table drifted from apps/stems/tiers.py. "
            f"tiers={tiers.GPU_USD_PER_S} farm={GPU_USD_PER_S}"
        )
    if tiers.DEFAULT_GPU != DEFAULT_GPU_KIND:
        raise SystemExit(
            f"error: default GPU drifted -- tiers says {tiers.DEFAULT_GPU!r}, "
            f"this farm says {DEFAULT_GPU_KIND!r}."
        )
    if tiers.MAX_CONCURRENT_GPUS != DEFAULT_MAX_CONTAINERS:
        raise SystemExit(
            f"error: concurrency drifted -- tiers says "
            f"{tiers.MAX_CONCURRENT_GPUS}, this farm says "
            f"{DEFAULT_MAX_CONTAINERS}."
        )
    _assert_stem_size_policy_mirror_matches()


def _assert_stem_size_policy_mirror_matches() -> None:
    """Keep mirrored Opus sizing identical to apps.stems.stem_size_policy."""
    from apps.stems import stem_size_policy as pol

    if (
        pol.STEM_PART_COUNT != _STEM_PART_COUNT
        or pol.OPUS_MIN_KBPS != _OPUS_MIN_KBPS
        or pol.OPUS_MAX_KBPS != _OPUS_MAX_KBPS
        or pol.LOSSY_SIZE_GATED != _LOSSY_SIZE_GATED
        or pol.CONTROL_CODECS != _CONTROL_CODECS
    ):
        raise SystemExit(
            "error: stem_size_policy constants drifted from the Modal mirror"
        )
    for kbps in (96.0, 128.0, 256.0, 320.0, 512.0):
        if pol.opus_kbps_per_stem(kbps) != _opus_kbps_per_stem(kbps):
            raise SystemExit(
                f"error: opus_kbps_per_stem({kbps}) drifted -- "
                f"policy={pol.opus_kbps_per_stem(kbps)} "
                f"farm={_opus_kbps_per_stem(kbps)}"
            )
        if pol.opus_ffmpeg_audio_args(kbps) != _opus_ffmpeg_audio_args(kbps):
            raise SystemExit(
                f"error: opus_ffmpeg_audio_args({kbps}) drifted from Modal mirror"
            )


def _signature_of(stat: os.stat_result) -> dict[str, int]:
    """``apps.vocals.cache.audio_signature``'s shape, from a stat already held.

    The feeder reads on a pool thread and stats there too, so it holds the stat
    that describes the exact generation whose bytes were sent. Re-statting the
    path later would reopen the iCloud re-materialisation race the signature
    exists to survive. Duplicating the shape is the price; the probe in
    ``_assert_contract_matches`` fails the run the moment the two disagree.
    """
    return {
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def compute_gap(data_dir: Path, refarm: bool = False) -> list[GapTrack]:
    """Playlist-member tracks with a real audio file and no cache entry yet,
    ordered LONGEST FIRST.

    ``refarm=True`` ignores the cache. The cache is keyed by stable_id ONLY,
    not by preset, so a track separated once at any rung is invisible to every
    later run -- which makes a deliberate re-run at a different tier impossible
    without this. Off by default: silently re-paying for work already done is
    the more expensive mistake of the two.
    """
    from apps.vocals import cache as vcache

    state_db = data_dir / "state" / "state.db"
    if not state_db.is_file():
        raise SystemExit(f"error: STATE_DB missing: {state_db}")
    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT DISTINCT t.stable_id, t.file_path, t.duration_ms "
            "FROM playlist_memberships m "
            "JOIN tracks t ON t.stable_id = m.stable_id "
            "WHERE t.file_path IS NOT NULL AND t.file_path != ''"
        ).fetchall()
    finally:
        connection.close()

    cached: set[str] = set()
    if not refarm:
        cached = {path.stem for path in vcache.cache_dir(data_dir).glob("*.json")}
    gap: list[GapTrack] = []
    for stable_id, file_path, duration_ms in rows:
        if stable_id in cached:
            continue
        path = Path(file_path)
        if not path.is_file():
            continue
        gap.append(
            GapTrack(
                stable_id=stable_id,
                audio_path=path,
                duration_ms=int(duration_ms or 0),
                size_bytes=path.stat().st_size,
            )
        )
    return sort_longest_first(gap)


def sort_longest_first(gap: list[GapTrack]) -> list[GapTrack]:
    """Longest-processing-time-first, the standard answer to head-of-line delay.

    ``.starmap`` runs with ``order_outputs=True``, so results are published in
    INPUT order: a long track sitting mid-batch makes every task finished
    behind it wait for it. Ordering long tracks first reduces the time that
    completed short tasks can remain blocked behind a long task.

    Putting the long tracks first means the batch's tail is short tasks, so the
    worst case is waiting on a short one. It costs nothing and needs no new
    measurement.

    WHY NOT just drop ``order_outputs=True``, which would remove the blocking
    entirely: with unordered outputs a returned exception carries no
    ``stable_id`` (there is no result dict to read it from), so a Modal-level
    failure could not be attributed to a track and would land in the failure
    list unnamed. Ordered outputs plus this sort keeps every failure
    attributable and takes most of the win.

    duration_ms is the real key; file size breaks ties and covers
    rows without duration metadata. stable_id makes the order deterministic so
    two runs of the same gap farm in the same sequence.
    """
    return sorted(
        gap,
        key=lambda t: (-t.duration_ms, -t.size_bytes, t.stable_id),
    )


def _inputs(
    batch: list[GapTrack],
    preset: Preset,
    sent: dict[str, tuple[dict[str, int], float]],
    stems_dest: str,
    r2_bucket: str,
    progress: Any,
    yielded_at: dict[str, float],
    stem_codec: str = DEFAULT_STEM_CODEC,
    tally: "FarmTally" | None = None,
    prefetch_depth: int = PREFETCH_DEPTH,
    prefetch_workers: int = PREFETCH_WORKERS,
) -> Iterator[tuple[Any, ...]]:
    """Feed .starmap from a thread-pool read-ahead, never from a blocking read.

    THIS GENERATOR IS THE FAN-OUT, and it is the half of the pipeline that a
    naive reading gets backwards. Modal's SYNC ``.starmap`` does NOT drain it
    on a background thread. ``modal/_utils/async_utils.py::run_async_gen``
    advances the event loop with ``runner.run(gen.asend(...))`` on OUR thread,
    one step per output, and ``sync_or_async_iter`` pulls this iterator inline
    -- its own comment reads "This intentionally could block the event loop for
    the duration of calling __iter__ and __next__". Every second spent inside
    this generator is a second in which Modal uploads nothing, dispatches
    nothing and collects nothing.

    Blocking source reads can delay upload and result collection. Read-ahead
    separates source I/O from Modal's result loop; concurrency should be
    measured over an explicit interval rather than inferred from a configured cap.

    ``read_ahead`` moves those reads onto a bounded thread pool, so a yield
    here costs a dict lookup on already-buffered bytes rather than a
    network-backed file read.

    The signature is still taken immediately AFTER that file's read and now in
    the SAME thread as it (``apps.vocals.prefetch._read_one``), which is what keeps
    the publication guard meaningful. iCloud assigns a NEW inode when it
    materialises an evicted file, so a signature captured any later can
    describe a different generation than the bytes actually sent. Reading
    ahead makes that race possible, hence
    ``_signature_of`` on the stat the reader already holds.
    """
    from apps.vocals.prefetch import read_ahead

    ready_files = read_ahead(
        [track.audio_path for track in batch],
        depth=prefetch_depth,
        workers=prefetch_workers,
        max_bytes=PREFETCH_MAX_BYTES,
    )
    resumed = time.perf_counter()
    for track, ready in zip(batch, ready_files, strict=False):
        # read_ahead promises input order; a misalignment would sign every
        # track with another track's file, so check rather than trust.
        if ready.path != track.audio_path:
            raise RuntimeError(
                f"read-ahead misaligned for {track.stable_id}: got {ready.path}, "
                f"expected {track.audio_path}"
            )
        # Time from resuming this generator to holding the item, i.e. exactly
        # how long Modal's event loop was stalled waiting for input. With the
        # read-ahead warm this is near zero; it is the number the fix moves.
        stall_s = time.perf_counter() - resumed
        audio_bytes = ready.data
        sent[track.stable_id] = (_signature_of(ready.stat), ready.stat.st_mtime)
        if tally is not None:
            tally.feed_stall_s += stall_s
        # NOTE: this event is named feed_stall, not upload, because `s` is the
        # feeder stall (event-loop time waiting for input), NOT wire transfer.
        # Nothing in this file times the starmap handoff or queue wait, so
        # summing this to test a bandwidth hypothesis answers a different
        # question and inverts the conclusion.
        progress.emit(
            "feed_stall",
            stable_id=track.stable_id,
            bytes=len(audio_bytes),
            s=round(stall_s, 3),
        )
        # Mac-side epoch at the moment this input left for Modal. Paired with
        # the container's own wall_span it decomposes the round trip; see
        # _record_latencies.
        yielded_at[track.stable_id] = time.time()
        handed = time.perf_counter()
        yield (
            audio_bytes,
            track.stable_id,
            track.audio_path.name,
            preset.model,
            preset.overlap,
            preset.shifts,
            stems_dest,
            str(track.audio_path),
            preset.stamp(),
            r2_bucket,
            stem_codec,
        )
        # Control only comes back here when Modal asks for the NEXT input, so
        # this interval is time MODAL's loop spent on our thread: queueing,
        # pumping inputs, collecting outputs. Together with feed_stall_s it
        # partitions the feeder's wall, which is what makes "the feeder is the
        # bottleneck" a falsifiable claim rather than a story.
        resumed = time.perf_counter()
        if tally is not None:
            tally.handoff_s += resumed - handed


# ----- local: run -------------------------------------------------------------


def _write_result(
    data_dir: Path,
    track: GapTrack,
    result: dict[str, Any],
    preset: Preset,
    signature: dict[str, int],
    audio_mtime: float,
) -> int:
    """Publish one result to the vocal cache; returns the region count.

    The envelope sidecar is written AFTER the entry, deliberately. A sidecar
    without an entry would be an orphan nothing ever reads; an entry without a
    sidecar is just an older-shaped entry, which readers already tolerate. So
    ordering it this way makes the only possible inconsistency the harmless
    one.
    """
    from apps.vocals import cache as vcache
    from apps.vocals import envelope as venv

    worker_result = dict(result)
    worker_result.pop("stable_id", None)
    worker_result["params"] = region_params(preset)
    worker_result["timings"]["container_s"] = worker_result.pop("container_s")
    # Popped, not persisted into the entry: these are raw bytes, and the whole
    # point of the sidecar is to keep them out of the JSON the webui parses per
    # listing row.
    envelope = worker_result.pop("envelope", None)
    entry = vcache.write_entry(
        vcache.cache_path(data_dir, track.stable_id),
        worker_result,
        track.audio_path,
        audio_mtime=audio_mtime,
        source_signature=signature,
        preset=preset.stamp(),
    )
    if envelope is not None:
        cache_dir = vcache.cache_dir(data_dir)
        venv.write_envelope(
            venv.envelope_path(cache_dir, track.stable_id),
            stable_id=track.stable_id,
            hop_s=envelope["hop_s"],
            frames=envelope["frames"],
            vocals_rms_bytes=envelope["vocals_rms"],
            mix_rms_bytes=envelope["mix_rms"],
            # SAME signature the entry was published under, so a sidecar that
            # has drifted from its entry is detectable rather than silent.
            audio_signature=signature,
        )
    return len(entry["regions"])


# ----- local: stem bundles -> bifrost2, never accumulating on the Mac ---------


def _remote_dirs(listing: str) -> set[str]:
    """Subdirectory names from a bifrost2 `dir` listing, minus . and ..

    Function-local import: module scope here stays stdlib + modal because the
    CONTAINER imports this module to find separate_track.
    """
    from scripts.b2listing import remote_dirs

    return remote_dirs(listing)


def _remote_sizes(listing: str) -> dict[str, int]:
    """{filename: bytes} from a bifrost2 `dir` listing. Shared with the
    R2 -> bifrost2 sync job; see scripts/b2listing.py."""
    from scripts.b2listing import remote_sizes

    return remote_sizes(listing)


def _open_store() -> Any:
    """The bifrost2 asset store, proven reachable before any GPU time is spent.

    Verify reachability before separation so a failed destination cannot leave
    the runner accumulating an unbounded local backlog.
    """
    from scripts.b2store import Store

    store = Store()
    store.df()  # raises if the tailnet or the box is unreachable
    return store


def _stem_manifest(
    stable_id: str,
    source_path: str,
    result: dict[str, Any],
    preset_stamp: dict[str, Any],
) -> dict[str, Any]:
    """Bundle manifest, schema 2.

    Takes a plain ``source_path`` string and an already-built ``preset_stamp``
    rather than a Path and a Preset, because the R2 destination builds this
    INSIDE the container, where neither a local Path nor the Preset dataclass
    means anything. Pure stdlib for the same reason.

    Delta from the v1 that apps/stems/artifacts.py validates:
    ``.flac`` filenames instead of ``.wav``, a ``preset`` block, and an
    ``audio`` block. That last one exists because v1's reader established
    cross-stem alignment by parsing RIFF headers, which cannot work on FLAC;
    carrying sample_rate/frame_count/channels in the manifest gives a future
    reader the same guarantee without decoding. NOTE: v1's reader rejects all
    three changes (schema_version is Literal[1], extra keys are forbidden, and
    file entries must end .wav), so these bundles are archive-only until it
    learns v2. They are pushed to bifrost2 and deleted locally, so nothing in
    the webui could have read them from disk regardless.
    """
    return {
        "schema_version": STEM_BUNDLE_SCHEMA,
        "stable_id": stable_id,
        "model": {"name": preset_stamp["model"], "version": DEMUCS_VERSION},
        "source": {
            "path": source_path,
            "sha256": result["source_sha256"],
        },
        "files": {
            part: f"{part}.{result.get('stem_ext', 'flac')}" for part in STEM_PARTS
        },
        "preset": preset_stamp,
        "audio": result["audio"],
    }


def _publish_stems_local(
    stable_id: str,
    audio_path: Path,
    result: dict[str, Any],
    preset: Preset,
    data_dir: Path,
) -> tuple[int, float]:
    """Write one bundle where the webui reads it. No push, no delete.

    Deliberately NOT routed through STEMS_OUTBOX: that staging dir exists so a
    bundle never outlives its upload, and here the bundle IS the deliverable.
    """
    started = time.perf_counter()
    bundle = data_dir / "state" / "stems" / stable_id
    bundle.mkdir(parents=True, exist_ok=True)
    written = 0
    ext = result.get("stem_ext", "flac")
    for part in STEM_PARTS:
        part_path = bundle / f"{part}.{ext}"
        part_path.write_bytes(result["stems"][part])
        written += part_path.stat().st_size
    manifest = bundle / "manifest.json"
    manifest.write_text(
        json.dumps(
            _stem_manifest(stable_id, str(audio_path), result, preset.stamp()),
            indent=2,
        )
        + "\n"
    )
    return written, time.perf_counter() - started


def _publish_stems(
    stable_id: str,
    audio_path: Path,
    result: dict[str, Any],
    preset: Preset,
    store: Any,
) -> tuple[int, float]:
    """Stage one bundle, push it to bifrost2, verify it, delete the local copy.

    Returns (bytes_pushed, transfer_s). The Mac has ~26 GiB free against a
    full-gap FLAC footprint near 45 GB, so a bundle must never outlive its
    upload. The local delete is gated on the remote byte sizes matching
    exactly: an unverified delete would silently trade a bundle for nothing.
    """
    bundle = STEMS_OUTBOX / stable_id
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)
    try:
        local_sizes: dict[str, int] = {}
        for part in STEM_PARTS:
            part_path = bundle / f"{part}.{result.get('stem_ext', 'flac')}"
            part_path.write_bytes(result["stems"][part])
            local_sizes[part_path.name] = part_path.stat().st_size
        manifest_path = bundle / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                _stem_manifest(stable_id, str(audio_path), result, preset.stamp()),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        local_sizes["manifest.json"] = manifest_path.stat().st_size

        # Namespaced by preset: a re-run at another rung must not clobber this
        # archive, and b2store.save overwrites a same-named remote path silently.
        remote_subdir = f"{STEMS_REMOTE_ROOT}/{preset.tag}"
        transfer0 = time.perf_counter()
        store.save(str(bundle), remote_subdir)
        transfer_s = time.perf_counter() - transfer0

        remote_sizes = _remote_sizes(store.ls(f"{remote_subdir}/{stable_id}"))
        if remote_sizes != local_sizes:
            raise RuntimeError(
                f"bifrost2 copy of {stable_id} does not match local: "
                f"remote {remote_sizes} vs local {local_sizes}"
            )
        return sum(local_sizes.values()), transfer_s
    finally:
        shutil.rmtree(bundle, ignore_errors=True)


def verify_remote_bundles(
    store: Any, preset: Preset, stable_ids: list[str]
) -> list[str]:
    """Which of ``stable_ids`` bifrost2 does NOT hold a bundle directory for.

    One listing of the preset root avoids a separate SSH handshake per track.

    This is a SECOND look, after the per-bundle size check that gated each
    local delete. It catches what that one structurally cannot: a bundle
    clobbered or removed later in the same run. "Pushed" must remain true at
    exit, not merely at the moment the transfer completed.
    """
    listing = store.ls(f"{STEMS_REMOTE_ROOT}/{preset.tag}")
    present = _remote_dirs(listing)
    return [stable_id for stable_id in stable_ids if stable_id not in present]


def _preflight_disk(batch: list[GapTrack], want_stems: bool) -> None:
    """Refuse to start without room for the largest bundle this batch can make.

    Stream-and-delete keeps one bundle on disk at a time, so the requirement
    is set by the single biggest track, not the batch. FLAC stems run about
    8x the size of a mp3 source (four full-rate 16-bit stems at ~42% FLAC
    compression against a ~320kbps original), and SLACK_BYTES leaves room for
    the cache writes and anything else on the volume.
    """
    free = shutil.disk_usage(REPO_ROOT).free
    if not want_stems:
        needed = SLACK_BYTES
        why = "region JSON only"
    else:
        largest = max(track.audio_path.stat().st_size for track in batch)
        needed = largest * STEM_SIZE_FACTOR + SLACK_BYTES
        why = (
            f"largest source {largest / 1e6:.0f} MB x{STEM_SIZE_FACTOR} for its "
            f"FLAC stems, plus {SLACK_BYTES / 1e9:.0f} GB slack"
        )
    if free < needed:
        raise SystemExit(
            f"error: {free / 1e9:.1f} GB free on {REPO_ROOT}, need "
            f"{needed / 1e9:.1f} GB ({why}). Stems are streamed to bifrost2 and "
            "deleted locally, but one bundle must fit while it is in flight. "
            "Free space, or run with --no-stems."
        )
    print(f"preflight: {free / 1e9:.1f} GB free, need {needed / 1e9:.1f} GB ({why})")


@dataclass
class PublishOutcome:
    """One finished transfer, drained by the main loop to keep the tally and
    the progress stream single-threaded."""

    stable_id: str
    bytes_pushed: int = 0
    transfer_s: float = 0.0
    error: str = ""


class PublishQueue:
    """Bounded worker pool that moves bifrost2 pushes OFF the result loop.

    Calling ``_publish_stems`` inline while draining results lets a slow
    transfer stall consumption of every completed GPU result behind it.

    BOUNDED, and small, on purpose. Bundles arrive as bytes already in RAM, so
    an unbounded queue converts a slow uplink into unbounded memory growth and
    then an OOM kill that loses the whole run. A full queue instead blocks the
    result loop, which is honest backpressure: it surfaces as "TRANSFER
    binding" in the dashboard, which is the true diagnosis. The real fix for
    that diagnosis is ``--stems-dest r2``, not a longer queue.

    Workers are threads because the work is scp - a subprocess wait, entirely
    I/O - so the GIL is released throughout and threads cost nothing here.
    """

    def __init__(self, store: Any, preset: Preset, workers: int, depth: int) -> None:
        import queue as _queue
        import threading as _threading

        self._store = store
        self._preset = preset
        self._inbox: Any = _queue.Queue(maxsize=depth)
        self.outcomes: Any = _queue.Queue()
        self._threads = [
            _threading.Thread(target=self._worker, daemon=True) for _ in range(workers)
        ]
        for thread in self._threads:
            thread.start()

    def _worker(self) -> None:
        while True:
            item = self._inbox.get()
            if item is None:  # shutdown sentinel, one per worker
                self._inbox.task_done()
                return
            stable_id, audio_path, result = item
            try:
                size, transfer_s = _publish_stems(
                    stable_id, audio_path, result, self._preset, self._store
                )
                self.outcomes.put(PublishOutcome(stable_id, size, transfer_s))
            except Exception as exc:
                self.outcomes.put(
                    PublishOutcome(
                        stable_id, error=f"publish: {type(exc).__name__}: {exc}"
                    )
                )
            finally:
                self._inbox.task_done()

    def submit(self, stable_id: str, audio_path: Path, result: dict[str, Any]) -> None:
        """Hand off a bundle. Blocks only when the queue is full."""
        self._inbox.put((stable_id, audio_path, result))

    def drain(self) -> None:
        """Wait for every queued bundle, then stop the workers."""
        self._inbox.join()
        for _ in self._threads:
            self._inbox.put(None)
        for thread in self._threads:
            thread.join(timeout=30)


def _eta(started: float, done: int, total: int) -> str:
    if done == 0:
        return "?"
    remaining = (time.perf_counter() - started) / done * (total - done)
    return f"{int(remaining // 60)}m{int(remaining % 60):02d}s"


@dataclass
class FarmTally:
    """Everything the run summary needs to state a measured claim."""

    written: int = 0
    container_s: float = 0.0
    bundle_bytes: int = 0
    transfer_s: float = 0.0
    feed_stall_s: float = 0.0
    # Time Modal's own event loop spent on our thread between yields.
    handoff_s: float = 0.0
    # The round trip, decomposed. See _record_latencies for what each means and
    # which of them clock skew can move.
    dispatch_s: float = 0.0
    return_s: float = 0.0
    round_trip_s: float = 0.0
    clock_skew_calls: int = 0
    pushed: list[str] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    # [start, end] epoch seconds per call, stamped inside the container that
    # ran it. The ONLY basis in this file for a concurrency claim.
    spans: list[tuple[float, float]] = field(default_factory=list)

    @property
    def bundles(self) -> int:
        return len(self.pushed)

    @property
    def concurrency(self) -> dict[str, float]:
        from scripts.farm_concurrency import concurrency_profile

        return concurrency_profile(self.spans)


def _record_latencies(
    result: dict[str, Any],
    yielded_at: float,
    received_at: float,
    tally: FarmTally,
) -> None:
    """Split one round trip into dispatch, container and return time.

    Until now every timer in this pipeline was INSIDE the container (load_s,
    separate_s, encode_s, container_s). Everything outside it -- upload, queue
    wait, cold start, the trip home, and the ordering delay from
    order_outputs=True -- can be separated using the container wall span.

        yielded_at --[dispatch]-- wall_span[0] --[container]-- wall_span[1]
                                                    --[return]-- received_at

    ``round_trip_s`` is measured entirely on the Mac and ``container_s``
    entirely inside the container, so BOTH are immune to clock skew between the
    two machines. The dispatch/return split is not: it differences a Mac clock
    against a container clock, so a skew of s seconds moves s from one side to
    the other and leaves their sum exact. A negative value is therefore a skew
    readout, not nonsense, and is counted rather than clamped away.

    Reading them: a large dispatch_s means inputs are waiting to reach a
    container (upload, queue, or cold start). A large return_s on a call that
    finished quickly means it sat in starmap's ordering buffer behind a longer
    call, which is the head-of-line signature sort_longest_first exists to
    blunt.
    """
    from scripts.farm_concurrency import span_of

    start, end = span_of(result)
    dispatch_s = start - yielded_at
    return_s = received_at - end
    round_trip_s = received_at - yielded_at
    timings = result.setdefault("timings", {})
    timings["dispatch_s"] = round(dispatch_s, 3)
    timings["return_s"] = round(return_s, 3)
    timings["round_trip_s"] = round(round_trip_s, 3)
    tally.dispatch_s += dispatch_s
    tally.return_s += return_s
    tally.round_trip_s += round_trip_s
    if dispatch_s < 0 or return_s < 0:
        tally.clock_skew_calls += 1


_QUIET: bool = False  # set by --dashboard; the Live region owns the terminal


def _say(message: str) -> None:
    """Per-track chatter, silenced while the dashboard owns the terminal.

    Nothing is lost when it is silenced: every line here corresponds to an
    event already in the JSONL stream, which is what the dashboard renders
    and what survives the run for post-hoc analysis.
    """
    if not _QUIET:
        print(message, flush=True)


def _start_dashboard(path: Path) -> Any:
    """Render the live dashboard on a daemon thread against ``path``.

    A thread rather than a subprocess so Ctrl-C reaches the farm rather than
    the renderer, and daemon so a crashed render can never outlive the run.
    Requires rich; the farm itself does not, hence the loud, specific failure.
    """
    import threading

    try:
        import rich  # noqa: F401
    except ImportError:
        raise SystemExit(
            "error: --dashboard needs rich, which is not a farm dependency.\n"
            "  Add it to the overlay: uv run --with modal --with rich python "
            "scripts/modal_vocal_farm.py ...\n"
            "  Or drop --dashboard and watch the stream instead: "
            "uv run scripts/farm_dashboard.py --latest"
        )
    from scripts.farm_dashboard import render_live

    global _QUIET
    _QUIET = True
    thread = threading.Thread(target=render_live, args=(path,), daemon=True)
    thread.start()
    return thread


@dataclass
class Pending:
    """A track whose regions are computed but whose bundle is still moving."""

    track: GapTrack
    result: dict[str, Any]
    signature: dict[str, int]
    audio_mtime: float


def run_farm(
    batch: list[GapTrack],
    preset: Preset,
    data_dir: Path,
    stems_dest: str,
    store: Any | None,
    progress: Any,
    r2_bucket: str = DEFAULT_R2_BUCKET,
    stem_codec: str = DEFAULT_STEM_CODEC,
    queue_workers: int = 2,
    queue_depth: int = 3,
    prefetch_depth: int = PREFETCH_DEPTH,
    prefetch_workers: int = PREFETCH_WORKERS,
) -> FarmTally:
    """Fan the batch out and publish both products as they arrive.

    NOTHING IN THIS LOOP WAITS ON A TRANSFER. For ``r2`` the container already
    shipped the stems before returning, so a result arrives publish-complete.
    For ``bifrost2`` the bundle is handed to ``PublishQueue`` and the loop
    moves straight to the next result.

    The old invariant survives the change: regions are never published for a
    track whose bundle was lost. It is preserved by DEFERRING the region
    write, not by blocking - a bifrost2 track parks in ``pending`` until its
    transfer outcome arrives, and only a successful outcome writes the JSON.
    Deferring costs nothing; blocking cost the whole pipeline.
    """
    from scripts.farm_concurrency import span_of

    sent: dict[str, tuple[dict[str, int], float]] = {}
    # Mac-side epoch per input, filled by _inputs as it yields.
    yielded_at: dict[str, float] = {}
    tally = FarmTally()
    started = time.perf_counter()
    pending: dict[str, Pending] = {}
    queue = (
        PublishQueue(store, preset, queue_workers, queue_depth)
        if stems_dest == "bifrost2"
        else None
    )

    def publish_regions(entry: Pending) -> None:
        """Write region JSON and record the win. Raises to the caller's guard."""
        regions = _write_result(
            data_dir,
            entry.track,
            entry.result,
            preset,
            entry.signature,
            entry.audio_mtime,
        )
        tally.written += 1
        progress.emit("written", stable_id=entry.track.stable_id, regions=regions)
        _say(
            f"  ok {entry.track.stable_id} regions={regions} "
            f"cov={entry.result['coverage_pct']}%"
        )

    def fail(stable_id: str, path: str, stage: str, reason: str) -> None:
        tally.failures.append({"stable_id": stable_id, "path": path, "error": reason})
        progress.emit("failed", stable_id=stable_id, stage=stage, error=reason)
        _say(f"  FAIL {stable_id} [{stage}] {reason}")

    def drain_outcomes(block: bool = False) -> None:
        """Absorb finished transfers. Called every iteration so the queue's
        results are folded in continuously rather than in a lump at the end."""
        import queue as _queue

        if queue is None:
            return
        while True:
            try:
                outcome = queue.outcomes.get(timeout=0.5 if block else 0)
            except _queue.Empty:
                return
            entry = pending.pop(outcome.stable_id)
            if outcome.error:
                fail(
                    outcome.stable_id,
                    str(entry.track.audio_path),
                    "stems",
                    outcome.error,
                )
                continue
            tally.pushed.append(outcome.stable_id)
            tally.bundle_bytes += outcome.bytes_pushed
            tally.transfer_s += outcome.transfer_s
            progress.emit(
                "stems",
                stable_id=outcome.stable_id,
                bytes=outcome.bytes_pushed,
                s=round(outcome.transfer_s, 2),
                dest="bifrost2",
            )
            try:
                publish_regions(entry)
            except Exception as exc:
                fail(
                    outcome.stable_id,
                    str(entry.track.audio_path),
                    "written",
                    f"write: {type(exc).__name__}: {exc}",
                )

    # Outputs are consumed by index rather than zipped against the batch: zip
    # abandons the generator once the shorter side ends, and Modal's async
    # teardown then floods stderr with GeneratorExit noise.
    results = separate_track.starmap(
        _inputs(
            batch,
            preset,
            sent,
            stems_dest,
            r2_bucket,
            progress,
            yielded_at,
            stem_codec,
            tally,
            prefetch_depth,
            prefetch_workers,
        ),
        order_outputs=True,
        return_exceptions=True,
    )
    for index, result in enumerate(results, start=1):
        received_at = time.time()
        track = batch[index - 1]
        drain_outcomes()
        if isinstance(result, BaseException):
            # No span: the call never reached a container, so it occupied none.
            fail(
                track.stable_id,
                str(track.audio_path),
                "gpu",
                f"modal: {type(result).__name__}: {result}",
            )
            continue
        tally.spans.append(span_of(result))
        _record_latencies(result, yielded_at[track.stable_id], received_at, tally)
        if "error" in result:
            tally.container_s += result.get("container_s", 0.0)
            fail(track.stable_id, str(track.audio_path), "gpu", result["error"])
            continue
        tally.container_s += result["container_s"]
        # order_outputs=True promises result i belongs to input i. Trust it
        # only with a check: a silent off-by-one would stamp every track
        # with another track's regions, and nothing downstream would notice.
        if result["stable_id"] != track.stable_id:
            raise RuntimeError(
                f"starmap output misaligned at index {index}: result is for "
                f"{result['stable_id']} but input was {track.stable_id}"
            )
        progress.emit(
            "gpu",
            stable_id=track.stable_id,
            separate_s=result["timings"]["separate_s"],
            container_s=result["container_s"],
            dispatch_s=result["timings"]["dispatch_s"],
            return_s=result["timings"]["return_s"],
            coverage_pct=result["coverage_pct"],
            regions=len(result["regions"]),
        )
        signature, audio_mtime = sent[track.stable_id]
        entry = Pending(track, result, signature, audio_mtime)

        if queue is not None:
            pending[track.stable_id] = entry
            progress.emit(
                "stems_queued",
                stable_id=track.stable_id,
                bytes=sum(len(b) for b in result["stems"].values()),
            )
            queue.submit(track.stable_id, track.audio_path, result)
            continue

        if stems_dest == "local":
            # Synchronous on purpose: a local write is a disk copy, not a wire
            # transfer, so a queue would add a thread and buy nothing.
            size, transfer_s = _publish_stems_local(
                track.stable_id, track.audio_path, result, preset, data_dir
            )
            tally.pushed.append(track.stable_id)
            tally.bundle_bytes += size
            tally.transfer_s += transfer_s
            progress.emit(
                "stems",
                stable_id=track.stable_id,
                bytes=size,
                s=round(transfer_s, 2),
                dest="local",
            )
            try:
                publish_regions(entry)
            except Exception as exc:
                fail(
                    track.stable_id,
                    str(track.audio_path),
                    "written",
                    f"write: {type(exc).__name__}: {exc}",
                )
            continue

        if stems_dest == "r2":
            upload = result["stem_upload"]
            tally.pushed.append(track.stable_id)
            tally.bundle_bytes += upload["bytes"]
            tally.transfer_s += upload["upload_s"]
            progress.emit(
                "stems_queued", stable_id=track.stable_id, bytes=upload["bytes"]
            )
            progress.emit(
                "stems",
                stable_id=track.stable_id,
                bytes=upload["bytes"],
                s=upload["upload_s"],
                dest="r2",
            )
        try:
            publish_regions(entry)
        except Exception as exc:
            fail(
                track.stable_id,
                str(track.audio_path),
                "written",
                f"write: {type(exc).__name__}: {exc}",
            )
        if index % 10 == 0:
            _say(f"  [{index}/{len(batch)}] eta={_eta(started, index, len(batch))}")

    if queue is not None:
        _say(f"draining {len(pending)} in-flight bundle(s)...")
        queue.drain()
        drain_outcomes()
        if pending:
            raise RuntimeError(
                f"publish queue drained but {len(pending)} bundles have no "
                f"outcome: {sorted(pending)[:5]}"
            )
    return tally


def rate_line(tally: FarmTally, tracks: int, wall_s: float) -> str:
    """One grep-able ``RATE key=value ...`` line per stage.

    The point of staging 10 -> 50 -> full is to MEASURE the rate before
    committing to the full gap, so the numbers have to leave the process in a
    form a shell runner can log and compare, not only as prose.

    ``observed_*_concurrency`` are the fan-out numbers. They replace a divide
    by ``DEFAULT_MAX_CONTAINERS`` that assumed the fan-out it was supposed to
    be reporting, and which made the binding-constraint verdict below
    unfalsifiable: dividing container seconds by a cap that was never observed
    understated GPU time by up to 10x and biased every verdict toward
    TRANSFER.
    """
    seen = tally.concurrency
    # Wall seconds during which containers were actually occupied. At true
    # 10-way fan-out this is roughly container_s / 10; when the fan-out fails
    # it approaches container_s, which is what makes the comparison honest.
    gpu_s_total = seen["busy_window_s"]
    fields: dict[str, Any] = {
        "stage_tracks": tracks,
        "written": tally.written,
        "failed": len(tally.failures),
        "wall_s": f"{wall_s:.1f}",
        "s_per_track": f"{wall_s / tracks:.2f}",
        "gpu_s_per_track": f"{tally.container_s / tracks:.2f}",
        "observed_peak_concurrency": f"{seen['peak']:.0f}",
        "observed_mean_concurrency": f"{seen['mean']:.2f}",
        "max_containers_configured": DEFAULT_MAX_CONTAINERS,
        # The round trip, decomposed. Everything outside the container used to
        # be one unmeasured residual; these four make it addressable.
        "feed_stall_s": f"{tally.feed_stall_s:.1f}",
        "handoff_s": f"{tally.handoff_s:.1f}",
        "dispatch_s_per_track": f"{tally.dispatch_s / tracks:.2f}",
        "return_s_per_track": f"{tally.return_s / tracks:.2f}",
        "round_trip_s_per_track": f"{tally.round_trip_s / tracks:.2f}",
        "clock_skew_calls": tally.clock_skew_calls,
        "bundles": tally.bundles,
    }
    if tally.bundles:
        fields["bundle_gb"] = f"{tally.bundle_bytes / 1e9:.2f}"
        fields["push_mb_s"] = f"{tally.bundle_bytes / 1e6 / tally.transfer_s:.1f}"
        fields["binding"] = "TRANSFER" if tally.transfer_s > gpu_s_total else "GPU"
    else:
        fields["binding"] = "GPU"
    return "RATE " + " ".join(f"{k}={v}" for k, v in fields.items())


def _resolve_preset(name: str, allow_unbaked: bool) -> Preset:
    if name not in PRESETS:
        raise SystemExit(
            f"error: unknown --preset {name!r}; known: {', '.join(sorted(PRESETS))}"
        )
    preset = PRESETS[name]
    if preset.model not in BAKED_MODELS and not allow_unbaked:
        raise SystemExit(
            f"error: preset {name!r} needs model {preset.model!r}, which is not "
            f"baked into the image (baked: {', '.join(BAKED_MODELS)}), so every "
            "cold container would re-download it. Pass --allow-unbaked-model to "
            "accept that."
        )
    return preset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/modal_vocal_farm.py",
        description="Fill the vocal-cache gap on Modal serverless L4 GPUs",
    )
    parser.add_argument(
        "--tier",
        choices=_farm_tier_choices(),
        default=None,
        help="separation product from apps/stems/tiers.py: S quick (live), "
        "M optimal (library default), L delicious (control/showcase). "
        "Sets --preset for you. Mutually exclusive with --preset.",
    )
    parser.add_argument(
        "--preset",
        default=None,
        help=f"quality rung by tag (default {DEFAULT_PRESET}, i.e. tier M). "
        "Use --tier unless you are deliberately running a rung that is "
        "not one of the three products.",
    )
    parser.add_argument(
        "--stem-codec",
        default=None,
        choices=tuple(sorted(STEM_CODECS)),
        help=f"stem encoding (default {DEFAULT_STEM_CODEC}). The GPU is not "
        "the only cost that matters here: the 1617 farmable tracks are "
        "153 GB at flac against 42 GB at opus, and with --stems-dest "
        "bifrost2 every byte crosses this Mac twice. "
        "Keep flac for anything that will be SCORED -- SI-SDR against a "
        "lossy stem measures the codec as well as the model.",
    )
    parser.add_argument(
        "--gpu",
        default=None,
        choices=tuple(sorted(GPU_USD_PER_S)),
        help=f"Modal card (default {DEFAULT_GPU_KIND}). Applied by re-exec "
        f"because @app.function resolves gpu= at import; see GPU_KIND. "
        "B200/B300 are absent on purpose: torch 2.5.1 predates Blackwell.",
    )
    parser.add_argument(
        "--only-stable-id",
        action="append",
        default=None,
        help="repeatable; farm ONLY these ids. Fails loudly if one is not in "
        "the gap, so a typo cannot silently farm the whole library.",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="cap the batch (0 = the whole gap)"
    )
    parser.add_argument(
        "--data-dir", type=Path, default=None, help="override the data dir"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the gap and the plan, contact Modal not at all",
    )
    parser.add_argument("--allow-unbaked-model", action="store_true")
    parser.add_argument(
        "--ignore-memory-gate",
        action="store_true",
        help="start even when scripts/mem_gate.py says BLOCK. The gate exists "
        "because publish holds queue-depth + queue-workers bundles in RAM.",
    )
    parser.add_argument(
        "--refarm",
        action="store_true",
        help="re-separate tracks that already have a cache entry. The cache is "
        "keyed by stable_id, NOT by preset, so without this a track "
        "farmed once at any rung can never be re-run at another one. "
        "Costs real money on work already done -- say it explicitly.",
    )
    parser.add_argument(
        "--stems-dest",
        choices=STEM_DESTS,
        default=DEFAULT_STEM_DEST,
        help="where the four FLAC stems go. 'r2' uploads from inside the "
        "container so the Mac never carries a stem byte (needs R2_* in "
        "the environment); 'bifrost2' returns them and pushes over scp "
        "from a background queue; 'none' skips them. demucs computes all "
        "four sources either way, so this trades transfer, not GPU.",
    )
    parser.add_argument(
        "--no-stems",
        dest="stems_dest",
        action="store_const",
        const="none",
        help="alias for --stems-dest none",
    )
    parser.add_argument(
        "--r2-bucket",
        default=DEFAULT_R2_BUCKET,
        help=f"R2 bucket for --stems-dest r2 (default {DEFAULT_R2_BUCKET})",
    )
    parser.add_argument(
        "--queue-workers",
        type=int,
        default=16,
        help="concurrent direct-scp pushes (default 16). Each worker holds "
        "a bundle in RAM; choose worker count and --queue-depth within "
        "the available memory budget.",
    )
    parser.add_argument(
        "--queue-depth",
        type=int,
        default=8,
        help="bundles allowed to wait in RAM for transfer (default 3). Small "
        "on purpose: bundles are in memory, so depth trades RAM for "
        "tolerance of a slow uplink.",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="render the live Rich dashboard in this terminal. The JSONL "
        "stream is written either way.",
    )
    parser.add_argument(
        "--no-require-remote-verify",
        dest="require_remote_verify",
        action="store_false",
        help="skip the end-of-stage assertion that bifrost2 still holds every "
        "bundle this stage pushed. On by default so a staged runner's "
        "exit-code guard stops escalation when the archive is short.",
    )
    parser.add_argument(
        "--prefetch-workers",
        type=int,
        default=PREFETCH_WORKERS,
        help=f"threads materialising audio ahead of dispatch (default "
        f"{PREFETCH_WORKERS}). --prefetch-workers 1 --prefetch-depth 1 is "
        "the SERIAL control arm: without it the read-ahead cannot be A/B "
        "tested and any claim that it helped is uncontrolled.",
    )
    parser.add_argument(
        "--prefetch-depth",
        type=int,
        default=PREFETCH_DEPTH,
        help=f"reads in flight ahead of dispatch (default {PREFETCH_DEPTH}). "
        "Memory is really bounded by the byte budget, not this.",
    )
    parser.set_defaults(require_remote_verify=True)
    return parser


def _preflight_memory(args: argparse.Namespace) -> None:
    """Refuse to start a run on a thrashing machine.

    THE GATE ALREADY EXISTED AND NOTHING CALLED IT. scripts/mem_gate.py was
    written for exactly this and had no caller anywhere in the repo, so the
    farm gated on DISK -- which has never killed a run -- and not on the
    resource that has.

    It matters here because publish residency is `queue_depth + queue_workers`
    bundles held as BYTES IN RAM, and each in-flight bundle is simultaneously
    in memory and staged on disk. At the defaults that is 24 slots; a flac arm
    of large tracks can put that well past a gigabyte on a machine that is
    already swapping.

    Advisory only for --stems-dest r2 and none: those never stage a bundle
    locally, so the residency argument does not apply and refusing would be
    theatre.
    """
    import subprocess

    stages_locally = args.stems_dest in ("bifrost2", "local")
    gate = REPO_ROOT / "scripts" / "mem_gate.py"
    if not gate.is_file():
        raise SystemExit(f"error: memory gate missing at {gate}")
    proc = subprocess.run(
        [sys.executable, str(gate), "check"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    verdict = (proc.stdout or proc.stderr or "").strip().splitlines()
    line = verdict[-1] if verdict else "(no output)"
    print(f"preflight: memory {line}")
    if proc.returncode == 0:
        return
    if not stages_locally:
        print(
            "  ... advisory only: this destination never stages a bundle "
            "locally, so publish residency does not apply"
        )
        return
    if args.ignore_memory_gate:
        print("  ... OVERRIDDEN by --ignore-memory-gate")
        return
    raise SystemExit(
        "error: refusing to start with the machine under memory pressure.\n"
        f"  {line}\n"
        f"  This run would hold up to {args.queue_depth + args.queue_workers} "
        "stem bundles in RAM at once (queue-depth + queue-workers), each also "
        "staged on disk.\n"
        "  Free memory, or lower --queue-workers/--queue-depth, or pass "
        "--ignore-memory-gate if you know better than the gate."
    )


def _check_r2_env() -> None:
    """Fail before any GPU time if a --stems-dest r2 run cannot possibly work.

    Loud and specific by design: the alternative is 900 containers each
    raising the same credential error after paying for a separation.
    """
    import os

    missing = [key for key in R2_ENV_KEYS if not os.environ.get(key)]
    if missing:
        raise SystemExit(
            f"error: --stems-dest r2 needs {missing} in the environment.\n"
            "  Run under: doppler run --project general --config dev_personal -- \\\n"
            "    uv run --with modal python scripts/modal_vocal_farm.py ...\n"
            "  If those secrets do not exist yet, R2 has to be enabled on the "
            "Cloudflare account and an R2 API token created first; then store "
            "the pair as R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY (and "
            "R2_ACCOUNT_ID) in Doppler. Until then use --stems-dest bifrost2."
        )


def _reexec_for_gpu(requested: str, argv: list[str] | None) -> None:
    """Restart this process with MDT_FARM_GPU set, so the decorator sees it.

    ``@app.function(gpu=GPU_KIND)`` is evaluated at IMPORT, long before argv is
    parsed, so --gpu cannot take effect in the process that read it. Re-exec is
    the honest fix: the alternative is a flag that silently does nothing, which
    is precisely the class of instrument fault this project keeps paying for.
    """
    if requested == GPU_KIND:
        return
    if os.environ.get("MDT_FARM_GPU_REEXEC") == "1":
        raise SystemExit(
            f"error: re-exec did not take -- asked for {requested!r}, "
            f"still running {GPU_KIND!r}. Refusing to farm on the wrong card."
        )
    env = dict(os.environ, **{GPU_ENV_VAR: requested, "MDT_FARM_GPU_REEXEC": "1"})
    argv_full = [sys.executable, "-m", "scripts.modal_vocal_farm"] + list(
        argv if argv is not None else sys.argv[1:]
    )
    print(f"[gpu] re-exec on {requested} (was {GPU_KIND})", flush=True)
    os.execve(sys.executable, argv_full, env)


def _farm_tier_choices() -> tuple[str, ...]:
    """Only rungs this farm can actually run. Derived from tiers.py, not typed.

    LOCAL is deliberately absent: it runs on the Mac, so offering it here would
    accept a flag that cannot do what it says.
    """
    from apps.stems.tiers import modal_tiers

    return tuple(t.key for t in modal_tiers())


def _resolve_tier_or_preset(args: argparse.Namespace) -> str:
    """--tier and --preset are two ways to say the same thing. Refuse both."""
    if args.tier and args.preset:
        raise SystemExit(
            f"error: --tier {args.tier} and --preset {args.preset} both given. "
            "Pick one; --tier is the product, --preset is the raw rung."
        )
    if args.tier:
        from apps.stems.tiers import get_tier

        return get_tier(args.tier).preset_tag
    return args.preset or DEFAULT_PRESET


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.gpu:
        _reexec_for_gpu(args.gpu, argv)
    _assert_contract_matches()
    from apps.shared.paths import DATA_DIR

    data_dir = args.data_dir if args.data_dir is not None else DATA_DIR
    preset = _resolve_preset(_resolve_tier_or_preset(args), args.allow_unbaked_model)
    # The codec belongs to the PRODUCT, not the invocation: tier L is the
    # control arm and must stay lossless, tier M is bulk and should not. An
    # explicit --stem-codec still wins, because a one-off eval run of M needs
    # to be able to ask for flac.
    if args.stem_codec is None:
        if args.tier:
            from apps.stems.tiers import get_tier

            args.stem_codec = get_tier(args.tier).codec
        else:
            args.stem_codec = DEFAULT_STEM_CODEC
    print(f"stem codec: {args.stem_codec}")

    gap = compute_gap(data_dir, refarm=args.refarm)
    if args.only_stable_id:
        wanted = set(args.only_stable_id)
        gap = [t for t in gap if t.stable_id in wanted]
        missing = wanted - {t.stable_id for t in gap}
        if missing:
            # Loud, because "not in the gap" has two very different causes and
            # silently farming the rest would hide both: the track is already
            # cached, or the id is wrong.
            raise SystemExit(
                f"error: {sorted(missing)} not in the gap. Either already "
                "cached at this preset, or the id is wrong. Refusing to farm a "
                "different set than the one asked for."
            )
    batch = gap[: args.limit] if args.limit > 0 else gap
    total_bytes = sum(track.audio_path.stat().st_size for track in batch)
    print(
        f"gap={len(gap)} batch={len(batch)} preset={preset.tag} "
        f"(rung {preset.rung}: {preset.model} overlap {preset.overlap} "
        f"shifts {preset.shifts}) upload={total_bytes / 1e9:.2f} GB "
        f"stems-dest={args.stems_dest}"
    )
    if not batch:
        print("nothing to do: the gap is empty")
        return 0
    if args.dry_run:
        for track in batch[:10]:
            print(f"  would farm {track.stable_id} {track.audio_path.name}")
        if len(batch) > 10:
            print(f"  ... and {len(batch) - 10} more")
        return 0

    # Preflights, cheapest and most likely to fail first. Every one of these
    # is a condition that would otherwise surface only after GPU time is spent.
    if args.stems_dest == "r2":
        _check_r2_env()
        print(f"preflight: R2 credentials present, bucket {args.r2_bucket}")
    # Only the bifrost2 path stages bundles on local disk; r2 never touches it.
    _preflight_disk(batch, args.stems_dest in ("bifrost2", "local"))
    _preflight_memory(args)
    store = _open_store() if args.stems_dest == "bifrost2" else None
    if store is not None:
        print("preflight: bifrost2 asset store reachable")

    from scripts import farm_progress

    run_id = farm_progress.new_run_id()
    progress_path = farm_progress.log_path(data_dir, run_id)
    print(f"progress: {progress_path}  (tail -f, or --dashboard)")

    wall0 = time.perf_counter()
    with farm_progress.ProgressLog(progress_path, run_id) as progress:
        progress.emit(
            "run_start",
            tracks=len(batch),
            preset=preset.tag,
            dest=args.stems_dest,
            max_containers=DEFAULT_MAX_CONTAINERS,
            source_bytes=total_bytes,
        )
        painter = _start_dashboard(progress_path) if args.dashboard else None
        with app.run():
            tally = run_farm(
                batch,
                preset,
                data_dir,
                args.stems_dest,
                store,
                progress,
                r2_bucket=args.r2_bucket,
                stem_codec=args.stem_codec,
                queue_workers=args.queue_workers,
                queue_depth=args.queue_depth,
                prefetch_depth=args.prefetch_depth,
                prefetch_workers=args.prefetch_workers,
            )
        wall_s = time.perf_counter() - wall0
        progress.emit(
            "run_end",
            written=tally.written,
            failed=len(tally.failures),
            wall_s=round(wall_s, 1),
            container_s=round(tally.container_s, 1),
            usd=round(tally.container_s * _gpu_usd_per_s(), 3),
        )
    if painter is not None:
        painter.join(timeout=10)

    FAILURES_DIR.mkdir(parents=True, exist_ok=True)
    failures_path = FAILURES_DIR / f"failures-{preset.tag}-{len(batch)}trk.json"
    failures_path.write_text(
        json.dumps(tally.failures, indent=1) + "\n", encoding="utf-8"
    )

    per_track_wall_s = wall_s / len(batch)
    print(
        f"\ndone: {tally.written} written, {len(tally.failures)} failed, "
        f"wall={wall_s:.1f}s ({per_track_wall_s:.2f}s/track)"
    )
    print(
        f"cost: amortised-wall ${per_track_wall_s * _gpu_usd_per_s():.4f}/track, "
        f"gpu-time ${tally.container_s * _gpu_usd_per_s() / len(batch):.4f}/track "
        f"(container_s total {tally.container_s:.0f}s) -> "
        f"${tally.container_s * _gpu_usd_per_s():.2f} for the batch"
    )
    seen = tally.concurrency
    print(
        f"fan-out: observed peak {seen['peak']:.0f} containers, mean "
        f"{seen['mean']:.2f} over a {seen['busy_window_s']:.0f}s busy window "
        f"({seen['calls']:.0f} calls, cap configured at "
        f"{DEFAULT_MAX_CONTAINERS}); local feed stalled the Modal event loop "
        f"for {tally.feed_stall_s:.1f}s total"
    )
    if tally.spans:
        n = len(tally.spans)
        print(
            f"round trip per track: dispatch {tally.dispatch_s / n:.2f}s + "
            f"container {tally.container_s / n:.2f}s + return "
            f"{tally.return_s / n:.2f}s = {tally.round_trip_s / n:.2f}s "
            f"(feeder: {tally.feed_stall_s:.1f}s stalled, "
            f"{tally.handoff_s:.1f}s in Modal's loop)"
        )
        if tally.clock_skew_calls:
            print(
                f"       NOTE {tally.clock_skew_calls} call(s) split negative: "
                "the Mac and container clocks disagree, so the dispatch/return "
                "SPLIT is shifted by the skew. Their sum, and container time, "
                "are unaffected."
            )
    if tally.bundles:
        gpu_s = seen["busy_window_s"]
        # For r2 the transfer seconds are CONCURRENT (each container uploads
        # its own bundle), so they cannot pace the run and the comparison is
        # meaningless; only the serial local uplink can be a binding
        # constraint. Saying "GPU" for r2 is a statement about the shape of
        # the pipeline, not a measurement of it.
        binding = (
            "GPU"
            if args.stems_dest == "r2"
            else ("TRANSFER" if tally.transfer_s > gpu_s else "GPU")
        )
        print(
            f"stems: {tally.bundles} bundles, "
            f"{tally.bundle_bytes / 1e9:.2f} GB to {args.stems_dest} at "
            f"{tally.bundle_bytes / 1e6 / tally.transfer_s:.1f} MB/s "
            f"({tally.transfer_s:.0f}s transferring vs a {gpu_s:.0f}s observed "
            f"busy window at mean {seen['mean']:.2f} containers) -> binding "
            f"constraint is {binding}"
        )
        if args.stems_dest == "r2":
            print(
                f"       remote: s3://{args.r2_bucket} objects content-"
                f"addressed as {R2_CONTENT_ADDRESSED_KEY} (cloudsync issue "
                "#1452; the Mac carried no stem bytes)"
            )
            print(
                "       note: scripts/r2_stem_sync.py still reads the legacy "
                "stems/<preset>/ layout and is the migration tool for those "
                "objects, not the archive rail for content-addressed ones"
            )
        else:
            print(f"       remote: D:/asset-store/{STEMS_REMOTE_ROOT}/{preset.tag}/")
    print(f"failures -> {failures_path}")
    for failure in tally.failures:
        print(f"  {failure['stable_id']}: {failure['error']}")

    print(rate_line(tally, len(batch), wall_s))

    if tally.written == 0:
        print("error: nothing was written this stage", file=sys.stderr)
        return 1
    if tally.pushed and args.require_remote_verify and store is not None:
        missing = verify_remote_bundles(store, preset, tally.pushed)
        if missing:
            print(
                f"error: bifrost2 is missing {len(missing)} of {len(tally.pushed)} "
                f"bundles this stage pushed: {missing[:5]}"
                f"{' ...' if len(missing) > 5 else ''}",
                file=sys.stderr,
            )
            return 1
        print(f"remote-verify: all {len(tally.pushed)} bundles present on bifrost2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
