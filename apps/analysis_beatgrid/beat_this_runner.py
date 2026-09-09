#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "beat-this==1.1.0",
#     "torch==2.14.0",
#     "numpy==1.26.4",
#     "soundfile==0.14.0",
# ]
# [tool.uv.sources]
# torch = [{ index = "pytorch-cpu", marker = "sys_platform == 'linux'" }]
# [[tool.uv.index]]
# name = "pytorch-cpu"
# url = "https://download.pytorch.org/whl/cpu"
# explicit = true
# ///
"""Beat This! 1.1.0 as the backfill beat/downbeat producer for the beatgrid lane.

A PEP 723 SCRIPT, NOT A REPO MODULE. torch and the model weights never enter
the repo venv (CLAUDE.md). Everything downstream of the beat times -- the
tempo fit, the octave policy, the changepoint detector, the flags -- lives in
`apps/analysis_beatgrid/{bpm,tempo_change,flags}.py`, which are pure stdlib and
run in the normal test lane. This file is only the part that needs a GPU-shaped
dependency tree, and it emits JSON so the two halves never have to share an
environment.

BAR NUMBERS LEFT THIS FILE FOR THAT REASON (schema 2, producer 1.1.0). It used
to call `beat_this.utils.infer_beat_numbers` and emit `beat_numbers` directly.
That helper counts upward between downbeats without wrapping, so a missed
downbeat produced numbers past 4: 24 of the 90 round-1 tracks did, one reaching
20, and rekordbox's PQTZ `n` is a position in a four-beat bar that no consumer
can place a 7 in (Codex P1 on PR #1514). Bar numbering is POLICY over beat
times, not model output, so it moved to `apps/analysis_beatgrid/bar_phase.py`
where it is stdlib, unit tested against synthetic grids with deliberately
missed downbeats, and shared by every consumer. Duplicating it here so the raw
JSON could keep the field would have put the one thing under test in the one
environment the test lane cannot import. The producer emits beats and
downbeats; `assign_bar_phase` turns them into 1..4 or says the phase is
unestablished.

WHY THE PEAK PICKER IS REIMPLEMENTED HERE. Beat This!'s own `Postprocessor`
hardcodes its keep-threshold at `logit > 0`, i.e. probability > 0.5, with no
argument to move it. That constant is the single actionable lever the research
survey identifies for this project's dynamic-tempo under-gridding: round 0
measured 6 of 137 dynamic tracks gridded not at all and 42 under-gridded, and
the SMC analysis attributes exactly that shape to activations dipping below the
threshold during tempo drift
(docs/research/beatgrid-and-segmentation-sota-20260906-a-beat-tracking.md
section 2.2). So `_pick_peaks` reproduces `Postprocessor.postp_minimal` step
for step with the threshold exposed.

That reimplementation is a risk, so it carries a control that can fail:
`--verify-postprocessor` runs BOTH implementations at the default threshold and
exits nonzero unless every beat time is identical. A benchmark row produced by
a peak picker that quietly drifted from the library's would be measuring this
file, not the model.

RAW ACTIVATIONS ARE KEPT. `Audio2Frames` returns framewise logits, and they are
written as float16 npz per track. They are the prerequisite for any later
threshold sweep or custom grid fitter, and re-deriving them costs a full model
pass while storing them costs about 240 KB for a 20 minute track. float16 is
ample: the postprocessor only ever compares these against a threshold and picks
local maxima.

EVERY INFERENCE DEPENDENCY IS PINNED EXACTLY, not just beat-this. The
reproducibility result this lane rests on -- beat times byte-identical across a
three week gap and a re-decode -- is a claim about ONE environment, and
`PRODUCER_VERSION` is what a later analysis record is keyed by. A range would
let a future release change inference or decoding while this file and the
producer version stayed still, so two runs recording the same producer version
could differ. beat-this was pinned first (Codex P1 on PR #1514); torch, numpy
and soundfile were left open in that fix and pinned in a later round of the
same review, because `torch>=2` resolves an arbitrary future torch and numpy
and soundfile were not even recorded in the payload. The pinned versions are
the ones the round-1 measurements were actually taken on, read out of the
resolved environment rather than chosen: torch 2.14.0, numpy 1.26.4,
soundfile 0.14.0. The resolved beat-this and torch versions are also stamped
into every output payload alongside the weights digest.

NOT RECORDED IN THE PAYLOAD: numpy and soundfile versions. They are pinned
here, so a run of THIS file is reproducible, but an artifact alone does not
name them. Adding them to the payload changes the artifact schema and belongs
with the record-writing lane rather than here.

PEAK MEMORY IS LENGTH-DEPENDENT. THE CHUNK BOUNDS THE TRANSFORMER, NOT THE
PROCESS. `Spect2Frames.spect2frames` calls `split_predict_aggregate(
chunk_size=1500, border_size=6, overlap_mode="keep_first")`, so the transformer
never sees more than 1500 frames (30 s) at once, and CHUNK_SIZE_FRAMES below is
asserted against the library's own constant at import so a version bump that
changes it fails loudly. That assertion still holds. What it does NOT do is
bound this process, and an earlier version of this docstring claimed otherwise
at "roughly 2 MB of spectrogram per audio minute": `load_audio` returns float64
before the resample, the log-mel spectrogram is full length, and
`split_predict_aggregate` accumulates every chunk's prediction in a list before
aggregating. MEASURED on this Mac, CPU, one synthetic click track per arm:
396.8 MB peak RSS at 2 minutes and 1617.0 MB at 20 minutes, about +68 MB per
audio minute above a ~330 MB floor. The short arm is what makes that a
measurement rather than a number. Consequence for the backfill: a 4-worker
drain over 20-minute material wants roughly 6.5 GB, so worker count is chosen
against track length, not core count. `--measure-rss` reports the peak on every
run so the figure stays a measurement. See specs/native-analysis-v1.md
section 4 and docs/perf/performance-register.md.

DEVICE: `auto` is mps, then cuda, then cpu. CPU is the REQUIRED path
(specs/native-analysis-v1.md section 4, "CPU-only path"), so `--device cpu`
must always work and the benchmark's honest timing figure is the CPU one; a
laptop analysing a library in the background is the target, not a GPU box.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sys
import time
from typing import Any

import numpy as np
import torch
from beat_this.inference import Audio2Frames, Postprocessor, load_audio

# ----- Named constants ----------------------------------------------------

PRODUCER = "own_beatgrid.backfill"
PRODUCER_VERSION = "1.1.0"

FPS = 50                      # Beat This! framewise prediction rate.
CHUNK_SIZE_FRAMES = 1500      # 30 s; the bound on inference memory.
DEFAULT_CHECKPOINT = "final0"

# Postprocessor.postp_minimal keeps a frame that is the maximum inside a
# max_pool1d(kernel=7, stride=1, padding=3) neighbourhood, which is +/- 3
# frames, and whose probability exceeds 0.5. Both are reproduced in _pick_peaks.
PEAK_POOL_KERNEL = 7
PEAK_POOL_PADDING = 3
DEFAULT_PEAK_THRESHOLD = 0.5


# ----- Device -------------------------------------------------------------


def resolve_checkpoint_sha256(checkpoint: str) -> tuple[str | None, str | None]:
    """`(resolved path, sha256)` of the weights actually loaded, or `(None, None)`.

    THE CHECKPOINT NAME IS NOT PROVENANCE. `final0` resolves to a file in the
    torch hub cache that can be replaced, and `--checkpoint` accepts an
    arbitrary path, so two runs recording the same name can have loaded
    different weights. The spec makes model sha256 part of every record
    (specs/native-analysis-v1.md sections 2 and 3) precisely because the weights
    are a direct input to every beat emitted. Reported as None rather than
    guessed when the file cannot be located, so an absent digest is visible
    instead of being a plausible-looking name (Codex P1 on PR #1514).
    """
    candidates = [checkpoint]
    if os.path.sep not in checkpoint:
        cache = os.path.join(
            torch.hub.get_dir(), "checkpoints", f"beat_this-{checkpoint}.ckpt"
        )
        candidates.append(cache)
    for path in candidates:
        if os.path.isfile(path):
            digest = hashlib.sha256()
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(block)
            return path, digest.hexdigest()
    return None, None


def resolve_device(requested: str) -> str:
    """`auto` picks mps, else cuda, else cpu. Anything else is taken literally.

    An explicit `--device mps` on a machine without MPS is an error rather than
    a silent fall back to CPU: a timing figure attributed to the wrong device
    is worse than a crash.
    """
    if requested != "auto":
        if requested == "mps" and not torch.backends.mps.is_available():
            raise SystemExit("[beatgrid] --device mps requested but MPS is not available")
        if requested == "cuda" and not torch.cuda.is_available():
            raise SystemExit("[beatgrid] --device cuda requested but CUDA is not available")
        return requested
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


# ----- Peak picking -------------------------------------------------------


def _pick_peaks(logits: torch.Tensor, threshold: float) -> np.ndarray:
    """Frame indices that are local maxima and clear `threshold` in probability.

    Mirrors `Postprocessor.postp_minimal`, including its adjacent-peak
    deduplication, with the threshold exposed. See `--verify-postprocessor`.
    """
    logit_threshold = float(np.log(threshold / (1.0 - threshold)))
    row = logits.detach().float().reshape(1, -1)
    pooled = torch.nn.functional.max_pool1d(
        row, PEAK_POOL_KERNEL, 1, PEAK_POOL_PADDING
    )
    peaks = (row == pooled) & (row > logit_threshold)
    frames = torch.nonzero(peaks[0]).cpu().numpy()[:, 0]
    if frames.size == 0:
        return frames
    # Deduplicate adjacent frames the way beat_this does: keep the centre of
    # each run of consecutive peak frames.
    groups = np.split(frames, np.where(np.diff(frames) > 1)[0] + 1)
    return np.array([round(float(np.mean(g))) for g in groups])


def _snap_downbeats_to_beats(beat_s: np.ndarray, downbeat_s: np.ndarray) -> np.ndarray:
    """Move every downbeat onto its nearest beat, as beat_this does.

    `apps.analysis_beatgrid.bar_phase.assign_bar_phase` REQUIRES every downbeat
    to also appear in the beat list and raises otherwise, so this is a
    correctness precondition and not cosmetics.
    """
    if beat_s.size == 0 or downbeat_s.size == 0:
        return np.array([], dtype=float)
    snapped = np.array([beat_s[int(np.argmin(np.abs(beat_s - d)))] for d in downbeat_s])
    return np.unique(snapped)


# ----- One track ----------------------------------------------------------


def analyze_one(
    audio_path: str,
    frames_model: Audio2Frames,
    threshold: float,
    activations_dir: str | None,
) -> dict[str, Any]:
    started = time.time()
    signal, sample_rate = load_audio(audio_path)
    # .tobytes() rather than handing hashlib the array: the buffer protocol
    # would hash whatever memory layout the array happens to have, so a
    # non-contiguous view of identical audio could fingerprint differently.
    decode_fingerprint = hashlib.sha256(
        np.ascontiguousarray(np.asarray(signal, dtype=np.float32)).tobytes()
    ).hexdigest()
    decoded_s = time.time()

    beat_logits, downbeat_logits = frames_model(signal, sample_rate)
    inferred_s = time.time()

    beat_frames = _pick_peaks(beat_logits, threshold)
    downbeat_frames = _pick_peaks(downbeat_logits, threshold)
    beat_s = beat_frames / FPS
    downbeat_s = _snap_downbeats_to_beats(beat_s, downbeat_frames / FPS)

    activation_path = None
    if activations_dir:
        os.makedirs(activations_dir, exist_ok=True)
        # The BASENAME alone collides: one run over two directories holding the
        # same filename wrote both tracks to one npz, the second overwriting the
        # first, while both JSON rows pointed at it. These are retained for
        # later threshold sweeps, so a silently shared file is a sweep computed
        # on the wrong activations (Codex P2 on PR #1514). The digest of the
        # full path disambiguates and stays stable across runs; the readable
        # stem is kept in front of it so the directory is still browsable.
        stem = os.path.splitext(os.path.basename(audio_path))[0]
        tag = hashlib.sha256(os.path.abspath(audio_path).encode("utf-8")).hexdigest()[:12]
        activation_path = os.path.join(activations_dir, f"{stem}.{tag}.npz")
        np.savez_compressed(
            activation_path,
            beat=beat_logits.detach().cpu().numpy().astype(np.float16),
            downbeat=downbeat_logits.detach().cpu().numpy().astype(np.float16),
            fps=np.int32(FPS),
        )

    # The peak activation is the model's own confidence and is what
    # apps.analysis_beatgrid.flags.evaluate_pulse consumes. Reported as a
    # probability so it is directly comparable with the threshold.
    peak_probability = float(torch.sigmoid(beat_logits.detach().float()).max())

    return {
        "audio": audio_path,
        "beats": [round(float(t), 5) for t in beat_s],
        "downbeats": [round(float(t), 5) for t in downbeat_s],
        "activation_peak": round(peak_probability, 6),
        "n_frames": int(beat_logits.shape[-1]),
        "activations_npz": activation_path,
        "decode_fingerprint": decode_fingerprint,
        "sample_rate": int(sample_rate),
        "decode_s": round(decoded_s - started, 3),
        "inference_s": round(inferred_s - decoded_s, 3),
        "runtime_s": round(time.time() - started, 3),
        "error": None,
    }


# ----- Controls -----------------------------------------------------------


def verify_against_library(audio_path: str, frames_model: Audio2Frames) -> int:
    """Assert `_pick_peaks` reproduces beat_this's own postprocessor exactly.

    A control that CAN fail: if the reimplementation drifts, or a beat_this
    release changes the peak rule, every benchmark row produced here would be
    measuring this file rather than the model, and this is what says so.
    """
    # Named on every run: with a manifest this is called once per fixture, and
    # an unattributed "identical" line cannot be traced back to a track.
    name = os.path.basename(audio_path)
    signal, sample_rate = load_audio(audio_path)
    beat_logits, downbeat_logits = frames_model(signal, sample_rate)

    library_beats, library_downbeats = Postprocessor(type="minimal", fps=FPS)(
        beat_logits, downbeat_logits
    )
    ours_beats = _pick_peaks(beat_logits, DEFAULT_PEAK_THRESHOLD) / FPS
    ours_downbeats = _snap_downbeats_to_beats(
        ours_beats, _pick_peaks(downbeat_logits, DEFAULT_PEAK_THRESHOLD) / FPS
    )

    ok = True
    for label, library, ours in (
        ("beats", np.asarray(library_beats), ours_beats),
        ("downbeats", np.asarray(library_downbeats), ours_downbeats),
    ):
        # rtol=0. `np.allclose`'s DEFAULT rtol of 1e-5 scales with the value
        # being compared, so on long audio this "identical" check silently
        # loosens: at 3600 s it would accept a 36 ms disagreement, larger than
        # the 20 ms frame period, and print it as identical (Codex P2 BLOCKING
        # on PR #1514). The control promises exact frame-time equivalence, so
        # the only tolerance allowed is the absolute float round-trip one.
        if library.shape != ours.shape or not np.allclose(library, ours, rtol=0.0, atol=1e-9):
            ok = False
            print(
                f"[verify] {name} MISMATCH on {label}: "
                f"library {library.shape} ours {ours.shape}",
                flush=True,
            )
            if library.shape == ours.shape:
                worst = float(np.max(np.abs(library - ours)))
                print(f"[verify]   worst difference {worst * 1000:.3f} ms", flush=True)
        else:
            print(
                f"[verify] {name} {label}: {len(ours)} identical to beat_this", flush=True
            )

    if not ok:
        print(
            f"[verify] FAILED on {name}: _pick_peaks no longer matches Postprocessor",
            flush=True,
        )
        return 1
    print(f"[verify] {name} OK at the default threshold", flush=True)
    return 0


def verify_every(paths: list[str], frames_model: Audio2Frames) -> int:
    """Run the control over EVERY path, not just the first.

    `run_round1.sh` step 3 passes a manifest, so verifying only `paths[0]` left
    a peak-picker regression triggered by any LATER track's ties or edges
    reporting the round green while the corpus was no longer equivalent to
    beat_this (Codex P1 BLOCKING on PR #1514). It does not stop at the first
    failure: WHICH tracks disagree is the finding, not that one did.
    """
    print(f"[verify] {len(paths)} track(s) to verify", flush=True)
    failed = [p for p in paths if verify_against_library(p, frames_model) != 0]
    if failed:
        print(
            f"[verify] FAILED on {len(failed)} of {len(paths)} track(s): "
            f"{[os.path.basename(p) for p in failed[:5]]}",
            flush=True,
        )
        return 1
    print(f"[verify] OK: all {len(paths)} track(s) match beat_this", flush=True)
    return 0


def _assert_chunk_size() -> None:
    """The memory claim rests on the library's chunk size, so read it, do not trust it."""
    import inspect

    from beat_this.inference import Spect2Frames

    source = inspect.getsource(Spect2Frames.spect2frames)
    if f"chunk_size={CHUNK_SIZE_FRAMES}" not in source:
        raise SystemExit(
            f"[beatgrid] beat_this no longer chunks at {CHUNK_SIZE_FRAMES} frames; the "
            "bounded-memory claim in this module's docstring is stale. Source:\n" + source
        )


# ----- Entry point --------------------------------------------------------


def _collect_paths(args: argparse.Namespace) -> list[str]:
    paths = list(args.audio)
    if args.manifest:
        with open(args.manifest, encoding="utf-8") as fh:
            manifest = json.load(fh)
        # Deliberate four-line copy of `scripts/beatbench/_harness`'s
        # `resolve_fixture_paths`, which this file cannot import: it runs in
        # its own PEP 723 torch environment with the repo off the path. A
        # portable bundle writes each `wav` relative to the manifest FILE,
        # while a bench manifest means relative to the repo root, so a bundle
        # says which it is and anything else keeps today's behavior. An
        # unrecognised declaration is refused rather than defaulted.
        declared = manifest.get("paths_relative_to")
        if declared not in (None, "manifest"):
            raise SystemExit(
                f"[beatgrid] {args.manifest} declares paths_relative_to "
                f"{declared!r}, which this runner cannot resolve"
            )
        base = os.path.dirname(os.path.abspath(args.manifest)) if declared else ""
        paths.extend(os.path.join(base, f["wav"]) for f in manifest["fixtures"])
    if not paths:
        raise SystemExit("[beatgrid] nothing to analyze: pass --audio or --manifest")
    return paths


def _peak_rss_mb() -> float:
    """Peak resident set in MB. ru_maxrss is BYTES on macOS and KILOBYTES on Linux."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def _load_model(checkpoint: str, device: str):
    """Load the weights and report WHICH weights, by digest, not by name."""
    started = time.time()
    model = Audio2Frames(checkpoint_path=checkpoint, device=device)
    load_s = round(time.time() - started, 3)
    path, sha256 = resolve_checkpoint_sha256(checkpoint)
    shown = f"{sha256[:16]}..." if sha256 else "SHA UNKNOWN"
    print(
        f"[beatgrid] model loaded in {load_s}s, weights {shown} "
        f"({path or 'path not resolved'})",
        flush=True,
    )
    return model, load_s, path, sha256


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audio", nargs="*", default=[], help="audio files to analyze")
    ap.add_argument("--manifest", help="beatbench fixtures.json; analyzes every wav in it")
    ap.add_argument("--out", required=True, help="JSON output path")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "mps", "cuda"])
    ap.add_argument("--checkpoint", default=DEFAULT_CHECKPOINT)
    ap.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_PEAK_THRESHOLD,
        help="peak-keep probability; beat_this hardcodes 0.5 and cannot be asked for another",
    )
    ap.add_argument("--activations-dir", help="write float16 npz activations here")
    ap.add_argument("--torch-threads", type=int, default=0, help="0 leaves torch alone")
    ap.add_argument("--measure-rss", action="store_true", help="report peak RSS in the output")
    ap.add_argument(
        "--verify-postprocessor",
        action="store_true",
        help="control: assert _pick_peaks matches beat_this, then exit",
    )
    args = ap.parse_args()

    if not 0.0 < args.threshold < 1.0:
        raise SystemExit(f"[beatgrid] --threshold must be in (0, 1), got {args.threshold}")

    _assert_chunk_size()
    if args.torch_threads:
        torch.set_num_threads(args.torch_threads)

    device = resolve_device(args.device)
    print(f"[beatgrid] device={device} checkpoint={args.checkpoint} "
          f"threshold={args.threshold}", flush=True)
    frames_model, load_s, checkpoint_path, checkpoint_sha256 = _load_model(
        args.checkpoint, device
    )

    paths = _collect_paths(args)

    if args.verify_postprocessor:
        return verify_every(paths, frames_model)

    results: dict[str, Any] = {}
    wall_started = time.time()
    for i, path in enumerate(paths, 1):
        try:
            results[path] = analyze_one(path, frames_model, args.threshold, args.activations_dir)
        # A failing track is a RESULT about the analyzer, not an accident to
        # abort on. Swallowing it silently would shrink the denominator and
        # quietly improve this candidate's scores, so it is recorded instead.
        except Exception as exc:  # noqa: BLE001
            results[path] = {
                "audio": path,
                "beats": [],
                "downbeats": [],
                "activation_peak": None,
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }
            print(f"[beatgrid] FAILED {path}: {results[path]['error']}", flush=True)
        if i % 25 == 0:
            print(f"[beatgrid]   {i}/{len(paths)} in {time.time() - wall_started:.0f}s", flush=True)
    wall_s = time.time() - wall_started

    peak_rss_mb = _peak_rss_mb() if args.measure_rss else None
    if peak_rss_mb is not None:
        print(f"[beatgrid] peak RSS {peak_rss_mb} MB", flush=True)

    ok = [r for r in results.values() if not r.get("error")]
    payload = {
        "schema": 2,
        "producer": PRODUCER,
        "producer_version": PRODUCER_VERSION,
        "beat_this_version": __import__("importlib.metadata", fromlist=["version"]).version(
            "beat-this"
        ),
        "torch_version": torch.__version__,
        "checkpoint": args.checkpoint,
        "checkpoint_path": checkpoint_path,
        "model_sha256": checkpoint_sha256,
        "device": device,
        "threshold": args.threshold,
        "chunk_size_frames": CHUNK_SIZE_FRAMES,
        "fps": FPS,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model_load_s": load_s,
        "wall_s": round(wall_s, 2),
        "peak_rss_mb": peak_rss_mb,
        "n_tracks": len(paths),
        "n_failed": len(paths) - len(ok),
        "results": results,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    print(
        f"[beatgrid] done: {len(ok)}/{len(paths)} in {wall_s:.0f}s -> {args.out}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
