#!/usr/bin/env python3
"""Modal GPU beat farm: the remote half of the v2 analysis compute plane.

WHY THIS EXISTS. `specs/native-analysis-v1.md` section 7 puts "multi-candidate
sweeps over 10k tracks" and "any training or fine-tune" on the ALWAYS REMOTE
line, split as "nucbox/agentbox for CPU, Modal for GPU". The CPU half is
proven: round 2 of `specs/beat-mapping-bench.md` ran 998 GTZAN clips on nucbox
at about 7.5 s per 30 s clip across 32 cores. The GPU half was a plan.
**nucbox has no NVIDIA GPU at all** (`nvidia-smi` finds no device, checked Thu
10 Sep 2026), so Modal is not the faster option for GPU work here, it is the
ONLY option, and v2's dynamic-grid fitter and any fine-tune are blocked behind
it. This proves it out.

THE PARITY CLAIM IS THE POINT, NOT THE SPEED. A farm that returns beats
quickly but not the SAME beats is worse than no farm, because it silently
splits the library into tracks analyzed here and tracks analyzed there. Three
things make the claim checkable rather than asserted:

  * **The same code runs.** `apps/analysis_beatgrid/beat_this_runner.py` is
    mounted into the image and `analyze_one` is called verbatim. Nothing is
    vendored, so the peak picker, the frame-to-time conversion and the decode
    fingerprint cannot drift from the local ones by being copied and forgotten.
  * **The same weights run.** The checkpoint is baked at image build time and
    its sha256 is asserted against `EXPECTED_CHECKPOINT_SHA256` IN THE BUILD.
    An upstream checkpoint change fails the build instead of quietly producing
    different beats.
  * **The output is scoreable by the same scorer.** Results are written in
    `beat_this_runner`'s own schema, so `scripts/beatbench/score_gtzan.py`
    reads a Modal run and a nucbox run identically and the comparison is one
    command rather than a bespoke diff.

WHAT EXACT EQUALITY IS AND IS NOT EXPECTED. The decode fingerprint SHOULD match
bit for bit: it is a hash of the decoded samples and decoding is deterministic.
The beat times were not expected to. cuDNN kernel selection and reduction order
differ from CPU, so logits differ in the last bits, and `_pick_peaks` takes a
strict local maximum and a threshold comparison, both of which can flip on a
tie. So `--compare` reports the fingerprint as a HARD equality and the beat
times against a tolerance, and it names which one failed. Reporting a
tolerance-based pass on the fingerprint would hide a decode divergence, which
is the one difference that would poison everything downstream.

MEASURED, AND THE PREDICTION WAS WRONG IN THE INTERESTING DIRECTION. 100 GTZAN
clips, Modal L4 against nucbox CPU, Thu 10 Sep 2026: fingerprints identical on
100 of 100 and beat times identical to 0.00 ms on 100 of 100. A clean binary
answer to a question that should have been messy is a reason to suspect the
instrument, so the comparator was mutated three ways against the real run --
one beat shifted 30 ms, three beats dropped from another clip, one fingerprint
corrupted -- and it caught all three (29.99 ms, 1100 ms via the SYMMETRIC scan
that a one-directional nearest-neighbour would have missed entirely, and a hard
fingerprint FAIL). The zero is real, and the reason is that the output is
frame-quantized: a beat time is `frame_index / 50`, so two runs agree exactly
or differ by a whole 20 ms frame, and nothing lives in between. Bit-level logit
noise is invisible unless it actually flips a peak, and over 100 clips none
did.

WHICH CARD, AND WHY IT IS RESOLVED AT IMPORT. Identical to
`scripts/modal_vocal_farm.py`: `@app.cls`'s `gpu=` is evaluated when this
module is imported, long before argv is parsed, so `--gpu` re-execs rather than
pretending to switch. Beat This! small is a tiny transformer, so the default is
L4 rather than the vocal farm's H100: the work is dominated by moving audio and
by the mel front end, not by matrix multiply, and paying for an H100 to idle is
how a farm gets expensive without getting faster.

Requirements (mini-PRD):
  ✔︎ ✅ the image bakes the beat_this checkpoint and FAILS THE BUILD if its
    sha256 is not EXPECTED_CHECKPOINT_SHA256.
    [if] upstream republishes final0 with different weights [then] the image
    build errors instead of returning different beats ⛔️
    [if] the digest matches [then] the build prints it and proceeds
    [if] the bake is removed [then] every container re-downloads, which the
    build log would show as a per-container cost
  ✔︎ ✅ `run` analyzes audio on a Modal GPU through the repo's own
    `analyze_one` and writes `beat_this_runner`'s schema.
    [if] the output is handed to score_gtzan.py [then] it scores without a
    shim ⛔️
    [if] a track fails [then] its entry carries `error` and the run continues
    [if] no audio is given [then] it exits nonzero rather than writing {}
  ✔︎ ✅ `compare` measures a Modal run against a CPU run of the same clips.
    [if] the decode fingerprints differ on any clip [then] it reports FAIL and
    names the clips, because decoding is deterministic ⛔️
    [if] beat times differ by more than --tolerance-ms [then] it reports the
    count and the worst clip rather than a mean that hides it
    [if] the two runs share no clip [then] it reports UNMEASURED, never a pass
  ✔︎ ✅ a run refuses to start above --max-usd.
    [if] the estimate exceeds the cap [then] it exits before creating the app
    [if] no cap is given [then] it uses DEFAULT_MAX_USD, never unbounded ⛔️

Usage:
    modal run scripts/modal_beat_farm.py::run --audio-dir <dir> --out out.json

    python -m scripts.beatbench.parity_report --gpu out.json --cpu cpu.json

The comparator lives in `scripts/beatbench/parity_report.py` rather than here.
It used to be a `compare` subcommand of this file, which meant it needed
`modal` importable even though it never calls it, because `@app.cls` and
`@app.local_entrypoint` are evaluated at module import; and it could not run
the way this docstring said, because `python scripts/modal_beat_farm.py`
puts `scripts/` on `sys.path` rather than the repository root. Anyone
there; the explicit `--with modal` is so a bare `python scripts/...` does not
fail with an import error that looks like a broken script.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import modal

# ----- Named constants ----------------------------------------------------

CHECKPOINT = "final0"
# The digest measured on nucbox for the run that produced
# ops/beatbench/round-2/gtzan-scores.json. Baking the weights is only worth
# anything if the baked ones are THESE ones.
EXPECTED_CHECKPOINT_SHA256 = (
    "8c328b45f59d8dd3dff219253ff6a8d6482be57d0133a29140e2febbf8eb8331"
)
TORCH_CACHE_DIR = "/root/.cache/torch"
# The exact versions apps/analysis_beatgrid/beat_this_runner.py pins in its
# PEP 723 header. Kept identical on purpose: a CPU/GPU parity claim is about the
# accelerator, so every other input has to be the same on both sides.
NUMPY_PIN = "1.26.4"
SOUNDFILE_PIN = "0.14.0"
# The DECODER, and the one dependency the two sides deliberately do NOT share:
# no common torchaudio exists across the CUDA image and the CPU wheel index, so
# it is 2.5.1 here against 2.11.0 on the runner. Pinned and measured anyway --
# `beat_this.inference.load_audio` calls `torchaudio.load` FIRST and only falls
# back to soundfile, so this produced every sample behind every
# `decode_fingerprint` (Codex P1 BLOCKING, PR #1660, discussion_r3977265421).
TORCHAUDIO_PIN = "2.5.1"
REMOTE_RUNNER = "/root/beat_this_runner.py"
LOCAL_RUNNER = "apps/analysis_beatgrid/beat_this_runner.py"

# Bumped whenever a change here can move a beat time. It rides into every
# shard header so two artifacts from different farm revisions cannot be
# merged as one benchmark.
PRODUCER_VERSION = "1.0.0"
GPU_ENV_VAR = "MDT_BEAT_FARM_GPU"
DEFAULT_GPU_KIND = "L4"
GPU_KIND: str = os.environ.get(GPU_ENV_VAR, DEFAULT_GPU_KIND)

# Published Modal rates, same table and same source as the vocal farm.
GPU_USD_PER_S: dict[str, float] = {
    "H100": 0.001097,
    "H200": 0.001261,
    "A100-80GB": 0.000694,
    "L40S": 0.000542,
    "L4": 0.000222,
}
# A ceiling, never an unbounded run. An overnight farm that silently costs
# real money is the failure mode this exists to prevent.
DEFAULT_MAX_USD = 5.0
# GPU seconds charged per second of AUDIO, not per file. Pricing per file was
# a defect, not a simplification: it valued a 7-minute DJ mix and a 30 s GTZAN
# clip identically, so a batch of ordinary 4:30 tracks passed a cap its real
# charge would blow through by an order of magnitude (Codex P1 BLOCKING, PR
# #1660). Derived from the round-2 measurement, 8 GPU-seconds for a 30 s clip,
# and left deliberately pessimistic so the estimate cannot under-price the cap.
ESTIMATE_GPU_S_PER_AUDIO_S = 8.0 / 30.0
# HALF a frame, not a whole one. Beat times are `frame_index / FPS` at 50 fps,
# so they are quantized to a 20 ms grid and two runs either agree EXACTLY or
# differ by a whole frame. A 20 ms default would therefore put a single flipped
# peak precisely on the boundary of the `>` comparison below, which is the one
# place a threshold must never sit. Measured Thu 10 Sep 2026: 100 clips, GPU
# against CPU, max gap 0.00 ms.
DEFAULT_TOLERANCE_MS = 10.0


def _audio_duration_s(path: Path) -> float:
    """Decoded duration in seconds, via ffprobe.

    Probing rather than assuming, and FAILING rather than falling back to a
    per-file constant when ffprobe is absent: a cost guard that silently stops
    measuring duration is a cost guard that silently stops guarding, and it
    would fail in the direction of spending money.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except FileNotFoundError as exc:
        raise SystemExit(
            "error: ffprobe not found, so this run cannot be priced by audio "
            "duration. Refusing to fall back to a per-file estimate, which "
            "under-prices anything longer than a 30 s clip."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"error: ffprobe could not read {path}: {exc.stderr}") from exc
    if not out:
        raise SystemExit(f"error: ffprobe reported no duration for {path}")
    return float(out)


def _gpu_usd_per_s() -> float:
    if GPU_KIND not in GPU_USD_PER_S:
        raise SystemExit(
            f"error: no published rate for {GPU_KIND!r}; "
            f"known: {', '.join(sorted(GPU_USD_PER_S))}"
        )
    return GPU_USD_PER_S[GPU_KIND]


# ----- Image --------------------------------------------------------------


def _bake_checkpoint() -> None:
    """Build step: fetch the checkpoint into the image AND verify its digest.

    The verification is the reason this is a build step rather than a runtime
    download. A digest checked at runtime tells you a run was wrong after
    paying for it; checked here it tells you the image is wrong before any
    container starts, and Modal caches the layer so it is checked once.
    """
    import hashlib

    from beat_this.inference import Audio2Frames

    Audio2Frames(checkpoint_path=CHECKPOINT, device="cpu")

    found = [
        os.path.join(root, name)
        for root, _, names in os.walk(TORCH_CACHE_DIR)
        for name in names
        if name.endswith(".ckpt")
    ]
    if not found:
        raise RuntimeError(
            f"[build] no .ckpt under {TORCH_CACHE_DIR} after loading {CHECKPOINT!r}: "
            "the bake did not happen, so every container would re-download"
        )
    digests = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in found}
    if EXPECTED_CHECKPOINT_SHA256 not in digests.values():
        raise RuntimeError(
            f"[build] baked weights do not match the round-2 checkpoint.\n"
            f"  expected {EXPECTED_CHECKPOINT_SHA256}\n"
            f"  found    {json.dumps(digests, indent=2)}\n"
            "Beats from this image would not be comparable with the committed "
            "CPU run. Refusing to build."
        )
    print(f"[build] baked {CHECKPOINT} sha256 {EXPECTED_CHECKPOINT_SHA256[:16]}...")


image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install(
        "beat-this==1.1.0",
        "torch==2.5.1",
        f"torchaudio=={TORCHAUDIO_PIN}",
        # PINNED, not ranged. `numpy<2` and `soundfile>=0.12` resolved whatever
        # was current on the day the image was built, so rebuilding the same
        # farm revision after a release could change decoding or inference
        # while every provenance field stayed identical, and two such runs would
        # merge as one benchmark (Codex P1 BLOCKING, PR #1660,
        # discussion_r3975199355). These are the versions
        # apps/analysis_beatgrid/beat_this_runner.py pins in its own PEP 723
        # header, so the CPU and GPU sides now resolve the same two libraries
        # rather than merely both being "recent enough".
        f"numpy=={NUMPY_PIN}",
        f"soundfile=={SOUNDFILE_PIN}",
    )
    .run_function(_bake_checkpoint)
    # copy=True bakes the runner into the image rather than mounting it, so a
    # container's code cannot change underneath a long fan-out mid-run.
    .add_local_file(LOCAL_RUNNER, REMOTE_RUNNER, copy=True)
)

app = modal.App(name="mdt-beat-farm", image=image)


# ----- Remote --------------------------------------------------------------


@app.cls(gpu=GPU_KIND, timeout=60 * 30, max_containers=10)
class BeatFarm:
    """One model load per container, reused across every clip it is handed.

    `@modal.enter` rather than loading inside the method: Beat This! small
    loads in well under a second, but at 998 clips a per-call load is 998 loads
    and the GPU sits idle through every one of them.
    """

    @modal.enter()
    def load(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("beat_this_runner", REMOTE_RUNNER)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not import the repo runner from {REMOTE_RUNNER}")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        self.runner = runner

        import torch

        if not torch.cuda.is_available():
            # Falling back to CPU here would return correct beats at a fraction
            # of the speed while the cost line still said GPU, so the timing
            # figure this farm exists to produce would be a fiction.
            raise RuntimeError(
                "no CUDA device in a GPU container: refusing to run and report "
                "a GPU figure measured on a CPU"
            )
        self.model, _, self.ckpt_path, self.ckpt_sha = runner._load_model(
            CHECKPOINT, "cuda"
        )
        if self.ckpt_sha != EXPECTED_CHECKPOINT_SHA256:
            raise RuntimeError(
                f"container loaded {self.ckpt_sha} but the image baked "
                f"{EXPECTED_CHECKPOINT_SHA256}"
            )

    @modal.method()
    def analyze(self, key: str, audio: bytes) -> dict[str, Any]:
        """`key` is the caller's identity for this input, and is echoed back.

        It is a path RELATIVE TO the corpus root, never a basename: two tracks
        called `01 - Intro.mp3` in different album directories collapse onto one
        result under a basename, and the artifact then reports fewer tracks than
        were analyzed with no way to tell which survived (Codex P1 BLOCKING, PR
        #1660).
        """
        import tempfile

        suffix = Path(key).suffix or ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
            fh.write(audio)
            path = fh.name
        try:
            result = self.runner.analyze_one(
                path, self.model, self.runner.DEFAULT_PEAK_THRESHOLD, None
            )
        # Broad on purpose: one unreadable clip must not kill a 998-way
        # fan-out, and the failure is RECORDED per clip rather than swallowed.
        except Exception as exc:
            return {"audio": key, "beats": [], "error": f"{type(exc).__name__}: {exc}"}
        finally:
            os.unlink(path)
        # `analyze_one` records the CONTAINER's temp path, which is meaningless
        # to the caller and unique per container. Echo the caller's own key.
        result["audio"] = key
        return result

    @modal.method()
    def versions(self) -> dict[str, str]:
        """The CONTAINER's dependency identity, read where the model actually ran.

        Composed remotely rather than locally on purpose: the driver runs on a
        Mac whose torch has nothing to do with the one that produced these
        beats, so a locally-read version would be a confident lie in the very
        field that exists to catch a dependency difference.
        """
        import hashlib
        import importlib.metadata

        # torch's VERSION comes from its distribution metadata rather than from
        # `import torch`. Importing it here put a torch object in this frame,
        # and modal pickles a remote exception together with its traceback, so
        # ANY fault in this method came back to the driver as "the 'torch'
        # module is not available in the local environment" instead of itself.
        # That is a failed measurement rendered as a different failure, and it
        # is why the committed artifact was never regenerated with provenance.
        # REPORTED, never judged here. An exception raised in this frame is
        # pickled with its traceback, and this frame holds `torch`, so the
        # driver fails to DESERIALIZE it and reports a missing local module
        # instead of the real fault. The pins are checked by `_refuse_dependency_drift`
        # on the driver, where the error can actually be read.
        return {
            "beat_this_version": importlib.metadata.version("beat-this"),
            "torch_version": importlib.metadata.version("torch"),
            # Pinned above AND measured here. A pin is a declaration; the
            # resolved version is the measurement, and only the measurement can
            # reveal an image built from a different lockfile. Checked against
            # the pin below rather than merely reported, because a field nobody
            # compares is a field that documents a drift instead of stopping it.
            "numpy_version": importlib.metadata.version("numpy"),
            "torchaudio_version": importlib.metadata.version("torchaudio"),
            "soundfile_version": importlib.metadata.version("soundfile"),
            # The BAKED runner's own identity. `PRODUCER_VERSION` below is this
            # file's, and it does not move when
            # apps/analysis_beatgrid/beat_this_runner.py changes, so shards from
            # either side of a runner edit compared equal and merged into a
            # hybrid benchmark (Codex P1 BLOCKING, PR #1660,
            # discussion_r3975279132). A digest of the bytes that actually ran
            # cannot drift the way a hand-maintained version string does.
            "runner_sha256": hashlib.sha256(
                Path(REMOTE_RUNNER).read_bytes()
            ).hexdigest(),
        }


# ----- Local entrypoints ---------------------------------------------------


def _refuse_dependency_drift(versions: dict[str, str]) -> None:
    """The container must have resolved the versions this farm pins.

    A pin is a declaration and the resolved version is the measurement; only
    the measurement can reveal an image built from a different lockfile. Ranged
    requirements (`numpy<2`, `soundfile>=0.12`) meant rebuilding the same farm
    revision after a release could change decoding or inference while every
    provenance field stayed identical, so two such runs merged as one benchmark
    (Codex P1 BLOCKING, PR #1660, discussion_r3975199355).

    Checked on the DRIVER rather than in the container: an exception raised
    remotely is pickled with its traceback, and the frame that reads these
    versions also holds `torch`, so the driver fails to deserialize it and
    reports a missing local module in place of the real fault.
    """
    drift = {
        name: {"pinned": pin, "resolved": versions.get(key)}
        for name, key, pin in (
            ("numpy", "numpy_version", NUMPY_PIN),
            ("soundfile", "soundfile_version", SOUNDFILE_PIN),
            ("torchaudio", "torchaudio_version", TORCHAUDIO_PIN),
        )
        if versions.get(key) != pin
    }
    if drift:
        raise SystemExit(
            "error: the container resolved a different inference dependency "
            f"from the one this farm pins, so its beats are not this farm's: {drift}"
        )


def _audio_paths(audio_dir: str, limit: int) -> list[Path]:
    paths = sorted(
        p for p in Path(audio_dir).rglob("*")
        # GTZAN repacks carry AppleDouble `._` sidecars that look like audio to
        # a suffix check and are not; they cost a container each if fed in.
        if p.suffix.lower() in {".wav", ".mp3", ".flac", ".m4a", ".aiff"}
        and not p.name.startswith("._")
    )
    if not paths:
        raise SystemExit(f"error: no audio under {audio_dir}")
    return paths[:limit] if limit else paths


def _payloads(paths: list[Path], keys: list[str]) -> Iterator[tuple[str, bytes]]:
    """`(key, bytes)` for each input, read a bounded window ahead of dispatch.

    A list comprehension here read the WHOLE corpus into memory before Modal
    could dispatch the first item, which is fine for 100 GTZAN clips and fatal
    at the 10k-track scale this farm is documented for: hundreds of gigabytes
    resident locally while remote workers sit idle (Codex P1 BLOCKING, PR
    #1660). `read_ahead` keeps a bounded number of files in flight and yields
    in input order, so dispatch starts on the first file and memory stays flat.
    """
    from apps.vocals.prefetch import read_ahead

    key_of = dict(zip((str(p) for p in paths), keys, strict=True))
    for ready in read_ahead(paths):
        yield key_of[str(ready.path)], ready.data


@app.local_entrypoint()
def run(
    audio_dir: str,
    out: str,
    limit: int = 0,
    max_usd: float = DEFAULT_MAX_USD,
) -> None:
    """Analyze every clip under `audio_dir` on a Modal GPU."""
    import time

    root = Path(audio_dir)
    paths = _audio_paths(audio_dir, limit)
    # Relative to the corpus root, so two identically-named tracks in different
    # directories stay distinct all the way into the artifact.
    keys = [str(p.relative_to(root)) for p in paths]

    audio_s = sum(_audio_duration_s(p) for p in paths)
    estimate = audio_s * ESTIMATE_GPU_S_PER_AUDIO_S * _gpu_usd_per_s()
    print(
        f"[farm] {len(paths)} files, {audio_s/60:.1f} min of audio on {GPU_KIND}, "
        f"estimated ${estimate:.2f} at ${_gpu_usd_per_s()*3600:.2f}/GPU-hour"
    )
    # BOTH SIDES OF THIS COMPARISON HAVE TO BE FINITE, and neither was checked:
    # `nan` makes every comparison false and `inf` cannot be exceeded, so a farm
    # documented as always bounded had no ceiling (Codex P2 BLOCKING, PR #1660,
    # discussion_r3977403982). The estimate is checked for the same reason from
    # the other side -- a NaN ffprobe duration propagates into it and then
    # compares false against every finite cap. Same defect as
    # `parity_report.finite_tolerance_ms`: a bound nothing can exceed is not one.
    if not math.isfinite(max_usd):
        raise SystemExit(
            f"error: --max-usd must be a finite dollar amount, not {max_usd!r}. "
            "A non-finite cap cannot be exceeded, so the run would proceed with "
            "no effective ceiling while reporting one."
        )
    if max_usd < 0:
        raise SystemExit(
            f"error: --max-usd must be non-negative, not {max_usd!r}. A negative "
            "cap rejects every run, including free ones, which is a different "
            "kind of wrong from an absent cap and should not be diagnosed as one."
        )
    if not math.isfinite(estimate):
        raise SystemExit(
            f"error: the cost estimate is {estimate!r}, which no cap can reject. "
            "One of the input durations did not measure; refusing rather than "
            "dispatching an unpriced fan-out."
        )
    if estimate > max_usd:
        raise SystemExit(
            f"error: estimate ${estimate:.2f} exceeds --max-usd ${max_usd:.2f}. "
            "Raise the cap deliberately or pass --limit."
        )

    started = time.time()
    farm = BeatFarm()
    results: dict[str, Any] = {}
    for result in farm.analyze.starmap(_payloads(paths, keys)):
        results[result["audio"]] = result
        if result.get("error"):
            print(f"[farm] FAILED {result['audio']}: {result['error']}", flush=True)
    wall = time.time() - started

    if len(results) != len(paths):
        # Two results cannot legitimately share a key now that keys are
        # relative paths, so a short dict means results were LOST, and an
        # artifact quietly missing tracks is exactly what the parity comparison
        # downstream would then read as evidence.
        raise SystemExit(
            f"error: sent {len(paths)} inputs but collected {len(results)} results. "
            "Refusing to write an artifact that has silently dropped tracks."
        )

    # Asked AFTER the fan-out so it lands on an already-warm container: this
    # class loads the model in @modal.enter, so a cold call for two strings
    # would pay a full container start and model load.
    versions = farm.versions.remote()
    _refuse_dependency_drift(versions)

    failed = sum(1 for r in results.values() if r.get("error"))
    payload = {
        "schema": 2,
        "producer": "modal_beat_farm",
        # The baked runner's digest is folded IN rather than reported beside,
        # so the existing merge guard refuses two shards from either side of a
        # runner edit without needing a new required provenance key (which
        # would make every pre-existing shard unmergeable). `PRODUCER_VERSION`
        # alone did not move when apps/analysis_beatgrid/beat_this_runner.py
        # changed -- as the documented round-3 +8 ms correction will change it
        # -- so such shards compared equal and merged into a hybrid benchmark
        # (Codex P1 BLOCKING, PR #1660, discussion_r3975279132).
        "producer_version": f"{PRODUCER_VERSION}+runner.{versions['runner_sha256'][:12]}",
        # Read off the CONTAINER that just ran, never hardcoded. These are the
        # only fields that can reveal two shards built against different
        # dependency versions, and emitting nothing was worse than emitting a
        # wrong value: the merge guard in score_gtzan.py read them with
        # `.get`, so absent-everywhere made every shard compare equal and the
        # guard passed vacuously (Codex P1 BLOCKING, PR #1660).
        "beat_this_version": versions["beat_this_version"],
        "torch_version": versions["torch_version"],
        "numpy_version": versions["numpy_version"],
        "torchaudio_version": versions["torchaudio_version"],
        "soundfile_version": versions["soundfile_version"],
        "runner_sha256": versions["runner_sha256"],
        "checkpoint": CHECKPOINT,
        "model_sha256": EXPECTED_CHECKPOINT_SHA256,
        "device": "cuda",
        "gpu": GPU_KIND,
        "threshold": 0.5,
        "fps": 50,
        "wall_s": round(wall, 1),
        "audio_s": round(audio_s, 1),
        "n_tracks": len(results),
        "n_failed": failed,
        "usd_estimate": round(estimate, 4),
        "results": results,
    }
    Path(out).write_text(json.dumps(payload, indent=1))
    print(
        f"[farm] {len(results)} clips in {wall:.1f}s "
        f"({wall/max(len(results),1):.2f}s/clip wall), {failed} failed -> {out}"
    )
    # THE SAME DEFECT AS THE DIRECT RUNNER'S, IN THE SIBLING PRODUCER: fixing the
    # instance left the class. `len(results) == len(paths)` still holds when
    # every row is an error, so a systemic decoder failure wrote a
    # complete-looking artifact, exited 0, and charged for it (Codex P1
    # BLOCKING, PR #1660, discussion_r3977660374). Narrow in the same way and
    # for the same reason: GTZAN's corrupt jazz.00054 fails on every legitimate
    # run, so this is "nothing succeeded", not "any failure".
    if paths and failed == len(results):
        raise SystemExit(
            f"error: all {len(results)} track(s) failed on the GPU, so this "
            f"artifact has no results despite costing about ${estimate:.2f}. "
            f"Written to {out} for diagnosis; exiting nonzero rather than "
            "reporting a completed run with nothing in it."
        )
