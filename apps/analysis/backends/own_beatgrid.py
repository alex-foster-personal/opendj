"""The own beatgrid BACKFILL producer, as an `apps.analysis` backend.

`python -m apps.analysis.run --backend own_beatgrid.backfill --files track.wav`
writes an `AnalysisRecord` v2 row under backend ``own_beatgrid.backfill``,
carrying the `beatgrid` lane block that `/anlz` then serves when that lane's
effective source is own. This is the FIRST producer to write an own record end
to end: `nav1-contract` (PR #1549) shipped the record, the store, the canonical
pointer and the projection on fixtures, and said so.

## Why the registry name is the full `own_beatgrid.backfill`

`apps/analysis/run.py` filters already-analyzed tracks with
``fetch_records_by_ids(backend=<the CLI's --backend value>)``, so the registry
key and the string a record is stored under have to be the SAME string or
``--only-missing`` silently matches nothing and every re-run re-analyzes the
whole library. The record's backend is fixed by the contract
(``own_<lane>.<producer>``, parsed and checked against the record body), so the
registry key is that, not a shorter alias. An alias would be a second name for
one thing whose only observable effect is to break the filter.

## Two invocation paths, neither hardcoded

`beat_this_runner.py` is a PEP 723 script. In a checkout it runs under
``uv run --no-project --script``; in a packaged build there is no ``uv``, and
the payload ships an interpreter carrying the runner's own locked PEP 723
closure (``scripts/payload_beatgrid.py``, NATIVE-10). Both are supported and
the choice is explicit:
set ``MDT_BEATGRID_RUNNER_PYTHON`` to that interpreter and the runner is invoked
as a plain script under it; leave it unset in a checkout and ``uv`` is used. A
missing ``uv`` with no interpreter named is a loud failure naming both, never a
silent attempt at the other path.

## The weights are resolved, verified, and passed as a PATH

`--checkpoint final0` would let Beat This! fetch weights over the network on a
fresh install (spec section 4, "no network after install"). This backend always
passes the local file `apps.analysis_beatgrid.weights` resolved and verified by
sha256, and stamps THAT measured digest into the record's ``model_sha256``.

## A track that was ANALYZED and could not be gridded still gets a record

`no_trackable_pulse`, an unestablished bar phase and a broken bar cadence all
produce a record whose `beatgrid` lane is ``status: failed`` with the named
reason, rather than no row at all. That is the whole point of the lane status:
`/anlz` can then say `failed` with a reason instead of `missing`, and the queue
does not keep re-analyzing a track that has already been answered.

Two cases are NOT that, and both raise instead. A file that VANISHED raises
:class:`~apps.analysis.backends.base.TrackVanished`: it was never attempted, so
recording an outcome for it would be a fabrication. A track the RUNNER ITSELF
errored on raises :class:`~apps.analysis.backends.base.TrackUnreadable`: its
error path records no `decode_fingerprint`, which the v1 record contract
requires on every own record precisely so the player's own decode can be checked
against it, and there is nothing honest to put in its place.

-Claude
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.analysis.pcm_fingerprint import (
    FingerprintUnavailable,
    canonical_decode_fingerprint,
    require_resampler,
)
from apps.analysis_beatgrid import activations
from apps.analysis_beatgrid.grid_fit import GRID_FIT_RAW
from apps.analysis_beatgrid.lane_payload import build_beatgrid_lane
from apps.analysis_beatgrid.version import LANE, PRODUCER, PRODUCER_VERSION

from ..lanes import LaneResult, own_backend
from ..record import AnalysisRecord
from . import register
from .base import BackendNotAvailable, TrackUnreadable, TrackVanished
from .genre_hint import library_genre

log = logging.getLogger("apps.analysis.backends.own_beatgrid")

#: The backend name, the registry key and the stored ``analysis.backend``
#: value, all one string. See the module docstring.
BACKEND_NAME = own_backend(LANE, PRODUCER)

RUNNER_PATH = (
    Path(__file__).resolve().parents[2] / "analysis_beatgrid" / "beat_this_runner.py"
)

#: Interpreter that already carries the runner's dependency closure. Set by a
#: packaged build; unset in a checkout, where ``uv`` resolves the PEP 723 block.
RUNNER_PYTHON_ENV = "MDT_BEATGRID_RUNNER_PYTHON"

#: ``cpu`` is the required path (spec section 4) and therefore the default;
#: a bench run may ask for ``mps`` or ``cuda`` explicitly.
DEVICE_ENV = "MDT_BEATGRID_DEVICE"
DEFAULT_DEVICE = "cpu"

#: Wall-clock ceiling for one track. Long enough for a 20 minute file on CPU
#: with a wide margin; a run that exceeds it has hung, and a hung backfill that
#: never returns is worse than one that says which track it stopped on.
TIMEOUT_ENV = "MDT_BEATGRID_TIMEOUT_S"

#: Which grid the lane serves: `raw` (the model's peak times, the default until
#: the grid-fit bench round is reviewed) or `line` (the fitted, BPM-rounded,
#: offset-corrected line from `apps.analysis_beatgrid.grid_fit`) or
#: `const_regions` (the same, with the line chosen by the constant-region
#: recipe in `apps.analysis_beatgrid.const_regions`). A fitted record says
#: which in its payload's `grid_fit` block.
GRID_FIT_ENV = "MDT_BEATGRID_GRID_FIT"
DEFAULT_TIMEOUT_S = 1800


class RunnerPayloadError(RuntimeError):
    """The runner produced output this backend refuses to turn into a record."""


#-----------------------------------------------------------------------------
# invocation
#-----------------------------------------------------------------------------

def runner_command(
    audio_path: Path,
    out_path: Path,
    checkpoint: Path,
    *,
    device: str,
    activations_dir: Path,
) -> list[str]:
    """The argv for one runner invocation, by whichever of the two paths applies."""
    args = [
        str(RUNNER_PATH),
        "--audio", str(audio_path),
        "--out", str(out_path),
        "--device", device,
        "--checkpoint", str(checkpoint),
        "--activations-dir", str(activations_dir),
    ]
    provisioned = os.environ.get(RUNNER_PYTHON_ENV, "").strip()
    if provisioned:
        return [provisioned, *args]
    uv = shutil.which("uv")
    if uv is None:
        raise BackendNotAvailable(
            f"{BACKEND_NAME} needs either {RUNNER_PYTHON_ENV} pointing at an "
            "interpreter carrying the analysis-backfill dependency closure, or "
            "`uv` on PATH to resolve the runner's PEP 723 block; neither is "
            "present, so beat_this_runner.py cannot be started at all"
        )
    return [uv, "run", "--no-project", "--script", *args]


def run_runner(audio_path: Path, checkpoint: Path, *, device: str) -> dict[str, Any]:
    """Run `beat_this_runner.py` over one file and return its parsed payload.

    A nonzero exit is a BACKEND fault, not a per-track one, and so is a
    timeout. The runner catches every per-track exception itself and records
    it in `results[...]["error"]` while still exiting 0 (that is deliberate on
    its side: swallowing a failing track would shrink the benchmark
    denominator), so the only way to reach a nonzero exit or a hang is a bad
    invocation, an environment that cannot load the model, or a wedged
    process. All three meet the next file the same way, which is exactly what
    :data:`~apps.analysis.run.EXIT_BACKEND_UNAVAILABLE` exists to tell a
    chunking caller to stop for.
    """
    timeout = float(os.environ.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT_S)
    activations_dir = activations.default_activations_dir()
    with tempfile.TemporaryDirectory(prefix="own-beatgrid-") as scratch:
        out_path = Path(scratch) / "beats.json"
        command = runner_command(
            audio_path, out_path, checkpoint, device=device, activations_dir=activations_dir
        )
        log.info("own_beatgrid: %s", " ".join(command))
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendNotAvailable(
                f"beat_this_runner.py did not finish {audio_path} within "
                f"{timeout:.0f}s (raise {TIMEOUT_ENV} if this machine is genuinely "
                "that slow); a runner that hangs will hang on the next file too"
            ) from exc
        except OSError as exc:
            raise BackendNotAvailable(
                f"could not launch {command[0]!r}: {exc}; the configured "
                f"{RUNNER_PYTHON_ENV} interpreter (or `uv`) is missing or not "
                "executable, which is a runner-availability fault, not a "
                f"per-track one, exactly like the {TIMEOUT_ENV} case above"
            ) from exc
        if completed.returncode != 0:
            raise BackendNotAvailable(
                f"beat_this_runner.py exited {completed.returncode} for "
                f"{audio_path}: {completed.stderr.strip()[-800:]}"
            )
        if not out_path.is_file():
            raise RunnerPayloadError(
                f"beat_this_runner.py exited 0 but wrote no {out_path}; stdout "
                f"tail: {completed.stdout.strip()[-400:]}"
            )
        try:
            payload = json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RunnerPayloadError(
                f"beat_this_runner.py exited 0 but wrote JSON this backend cannot "
                f"parse from {out_path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RunnerPayloadError(
                f"beat_this_runner.py exited 0 but wrote a {type(payload).__name__}, "
                "not an object"
            )
        for result in payload.get("results", {}).values():
            if result.get("error"):
                continue
            if result.get("n_frames") is None:
                continue
            npz = result.get("activations_npz")
            if not isinstance(npz, str) or not Path(npz).is_file():
                raise RunnerPayloadError(
                    f"runner result produced logits but activations_npz "
                    f"{npz!r} is not a readable file; producer contract drift"
                )
        return payload


#-----------------------------------------------------------------------------
# payload -> record
#-----------------------------------------------------------------------------

def _require(payload: dict[str, Any], key: str) -> Any:
    if key not in payload or payload[key] is None:
        raise RunnerPayloadError(
            f"runner payload has no {key!r}; got {sorted(payload)}"
        )
    return payload[key]


def _one_result(payload: dict[str, Any], audio_path: Path) -> dict[str, Any]:
    """The single track's result, matched by path and refusing an ambiguous set."""
    results = _require(payload, "results")
    if len(results) != 1:
        raise RunnerPayloadError(
            f"expected exactly one result for {audio_path}, got {len(results)}; "
            "this backend analyzes one track per invocation and cannot tell "
            "which row belongs to it otherwise"
        )
    return next(iter(results.values()))


def _duration_s(
    result: dict[str, Any], *, lane_ok: bool, fps: float, audio_path: Path
) -> float:
    """Track duration from the runner's frame count, or 0.0 for a failed track.

    NOT a hidden default in either branch. A run that produced a grid also
    produced framewise activations, so `n_frames` is present and its absence is
    a malformed payload rather than a value to guess at. A run that FAILED may
    never have decoded the file at all -- the runner's own error path records
    beats, downbeats, an activation peak and the error, and nothing else -- so
    there is no duration to state and 0.0 is the honest "not measured" for a
    NOT NULL REAL column whose value no own reader consults (`canonical.py`
    projects from `lanes`, never from this).
    """
    n_frames = result.get("n_frames")
    if n_frames is None:
        if lane_ok:
            raise RunnerPayloadError(
                f"runner result for {audio_path} produced a grid but records no "
                "n_frames, so the track duration cannot be derived; a payload "
                "that measured beats measured frames"
            )
        return 0.0
    if fps <= 0:
        raise RunnerPayloadError(f"runner payload declares fps {fps!r}, which is not a rate")
    return float(n_frames) / fps


def _sample_rate(
    result: dict[str, Any], *, lane_ok: bool, audio_path: Path
) -> int:
    """The decode's sample rate, or 0 when the lane failed before it mattered.

    The sibling of :func:`_duration_s`, and it exists because the rule was
    applied to one of the two and not the other: `int(result.get(...) or 0)`
    turned a payload that never states a rate into a record claiming 0 Hz,
    which is not a sample rate (Sol P1 BLOCKING, PR #1587). A run that produced
    a grid decoded audio, so its rate is present and its absence is a malformed
    payload; a run whose lane FAILED may have decoded and still found no pulse,
    in which case the rate is present too, but the runner's own shapes do not
    guarantee it there, and no own reader consults this column.
    """
    rate = result.get("sample_rate")
    if rate is None:
        if lane_ok:
            raise RunnerPayloadError(
                f"runner result for {audio_path} produced a grid but records no "
                "sample_rate; a payload that decoded audio decoded it at a rate"
            )
        return 0
    # Checked on BOTH branches. The 0 above is the ABSENT sentinel for a lane
    # that failed; a failed lane that explicitly STATES a rate of 0 or -1 is
    # malformed measured metadata wearing that sentinel's clothes, and letting
    # it through would make the two indistinguishable in the stored row
    # (Sol P2, PR #1587).
    if int(rate) <= 0:
        raise RunnerPayloadError(
            f"runner result for {audio_path} states sample_rate {rate!r}, which "
            "is not a rate; a rate this producer did not measure is absent, "
            "never zero"
        )
    return int(rate)


def record_from_payload(
    payload: dict[str, Any],
    *,
    stable_id: str,
    audio_path: Path,
    model_sha256: str,
    decode_fingerprint: str,
    grid_fit: str = GRID_FIT_RAW,
    genre: str | None = None,
) -> AnalysisRecord:
    """Turn one runner payload into one v2 record. Pure; raises, never guesses.

    ``decode_fingerprint`` is the CANONICAL one, taken by the caller from the
    audio itself (`apps.analysis.pcm_fingerprint`). It is a parameter rather
    than something measured here so this function stays pure and offline: the
    runner payload's own `decode_fingerprint` is a hash of Beat This's float32
    model input, which no other decoder reproduces, so stamping it would fail
    the cross-host comparison the field exists for on byte-identical audio
    (Codex P1 BLOCKING, PR #1587).

    ``model_sha256`` is the digest `apps.analysis_beatgrid.weights` MEASURED on
    the checkpoint file it resolved, not a constant and not the payload's own
    claim: the payload is compared against it, so a run that loaded different
    weights from the ones this process verified is refused rather than recorded.
    """
    stated_producer = _require(payload, "producer")
    if stated_producer != BACKEND_NAME:
        raise RunnerPayloadError(
            f"runner payload names producer {stated_producer!r}, not {BACKEND_NAME!r}"
        )
    stated_version = _require(payload, "producer_version")
    if stated_version != PRODUCER_VERSION:
        raise RunnerPayloadError(
            f"runner payload was produced by version {stated_version!r} but this "
            f"package declares {PRODUCER_VERSION!r}; a record keyed by a version "
            "that did not produce it cannot be re-queued on a version bump"
        )
    stated_model = _require(payload, "model_sha256")
    if stated_model != model_sha256:
        raise RunnerPayloadError(
            f"runner loaded weights {stated_model} but this process verified "
            f"{model_sha256}; the record's model_sha256 must name the weights "
            "the beats actually came from"
        )
    threshold = float(_require(payload, "threshold"))
    fps = float(_require(payload, "fps"))

    result = _one_result(payload, audio_path)
    runner_error = result.get("error")
    if runner_error:
        # A track the RUNNER itself could not get through is not a lane result:
        # its error path records no `decode_fingerprint`, and the v1 record
        # contract requires one on every own record precisely so the player's
        # own decode can be checked against it. There is nothing to fingerprint
        # and nothing honest to put in its place, so this is a per-FILE failure
        # rather than a `failed` lane block (Sol P1 BLOCKING, PR #1587).
        # `build_beatgrid_lane` still maps the same condition to a `failed`
        # lane for the bench path, which converts artifacts and writes no
        # records.
        raise TrackUnreadable(
            f"beat_this_runner.py could not analyze {audio_path}: {runner_error}"
        )
    lane = build_beatgrid_lane(result, threshold=threshold, grid_fit=grid_fit, genre=genre)
    # The runner reports the sha256 of ITS OWN model input: Beat This's
    # `load_audio` float32 at whatever rate it chose. The record may not carry
    # that. `decode_fingerprint` is defined over the canonical decode (44100
    # Hz, mono, s16, soxr) precisely so a second host, or the player about to
    # use this grid, can recompute it; a model-input hash is reproducible by
    # nothing but this runner, so the comparison the field exists for would
    # fail on byte-identical audio (Codex P1 BLOCKING, PR #1587). Still
    # REQUIRED from the payload, because a runner that stopped emitting it is
    # a schema change worth failing on, and its value is what the parity gate
    # compares when two hosts disagree about the model input rather than the
    # decode.
    _require(result, "decode_fingerprint")
    duration_s = _duration_s(result, lane_ok=lane.ok, fps=fps, audio_path=audio_path)
    sample_rate = _sample_rate(result, lane_ok=lane.ok, audio_path=audio_path)

    features_blob: dict[str, Any] = {}
    activations_npz = result.get("activations_npz")
    if activations_npz:
        track_fps = result.get("fps", fps)
        features_blob["activations"] = {
            "npz": activations_npz,
            "fps": int(track_fps),
        }

    # The pre-v2 scalar columns. This producer measures no key and no energy,
    # so it states nothing rather than a plausible-looking default; the lane
    # block is what every own reader consults (canonical.py projects from
    # `lanes`, never from these columns).
    return AnalysisRecord(
        stable_id=stable_id,
        backend=BACKEND_NAME,
        backend_version=PRODUCER_VERSION,
        analyzed_at=datetime.now(UTC),
        duration_s=duration_s,
        sample_rate=sample_rate,
        bpm=float(lane.payload["bpm"]) if lane.ok else 0.0,
        bpm_confidence=float(lane.payload["bpm_confidence"]) if lane.ok else 0.0,
        key_camelot="",
        key_openkey="",
        key_confidence=0.0,
        energy=0,
        energy_source="inferred",
        producer=PRODUCER,
        producer_version=PRODUCER_VERSION,
        uses_model=True,
        model_sha256=f"sha256:{model_sha256}",
        decode_fingerprint=f"sha256:{decode_fingerprint}",
        features_blob=features_blob,
        lanes={
            LANE: LaneResult(
                status=lane.status,
                reason=lane.reason,
                confidence=lane.confidence,
                payload=lane.payload,
            )
        },
    )


#-----------------------------------------------------------------------------
# backend
#-----------------------------------------------------------------------------


def _fingerprint_or_unreadable(audio_path: Path) -> str:
    """Canonical decode fingerprint, or a per-FILE failure by name."""
    try:
        return canonical_decode_fingerprint(audio_path)
    except FingerprintUnavailable as exc:
        # A resampler this host cannot run was refused in `analyze` before any
        # track was touched, so reaching here means THIS file failed to
        # decode -- unless a sync, relink or deletion raced the decode and
        # removed it in between, in which case it was never really attempted
        # and must be retried rather than recorded as bad audio (Codex P2
        # BLOCKING, PR #1587).
        if not audio_path.exists():
            raise TrackVanished(
                f"{audio_path} vanished while its fingerprint was being taken"
            ) from None
        raise TrackUnreadable(
            f"the canonical decode fingerprint could not be taken for {audio_path}: {exc}"
        ) from None


class OwnBeatgridBackfillBackend:
    """`apps.analysis.backends.base.AnalyzerBackend` for the own beatgrid lane."""

    name: str = BACKEND_NAME
    version: str = PRODUCER_VERSION

    @classmethod
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:
        from apps.analysis_beatgrid import weights

        audio_path = Path(path)
        if not audio_path.exists():
            raise TrackVanished(f"{audio_path} was gone before the runner opened it")
        try:
            checkpoint, model_sha256 = weights.resolve_checkpoint()
        except weights.WeightsError as exc:
            raise BackendNotAvailable(str(exc)) from exc
        try:
            # Checked up front, not per track: without a working ffmpeg
            # resampler EVERY record fails its fingerprint, and a host-wide
            # gap reported once per file reads as thousands of unreadable
            # tracks. The probe runs the real filter chain, so it fails here
            # for a build that merely accepts the option name.
            require_resampler()
        except FingerprintUnavailable as exc:
            raise BackendNotAvailable(
                f"the canonical decode fingerprint cannot be taken on this host: {exc}"
            ) from exc
        device = os.environ.get(DEVICE_ENV, "").strip() or DEFAULT_DEVICE
        # Fingerprint BEFORE the runner, because the record must name the
        # bytes the beats were derived FROM. Taken afterwards it names
        # whatever is on disk when the runner happens to finish, which for a
        # file replaced mid-run is a record claiming provenance for audio that
        # did not produce its grid - and it would claim it CONVINCINGLY, since
        # a later cross-host comparison against the new bytes would agree
        # while the beats it vouches for came from the old ones (Codex P2
        # BLOCKING, PR #1587).
        decode_fingerprint = _fingerprint_or_unreadable(audio_path)
        payload = run_runner(audio_path, checkpoint, device=device)
        # NO conversion guard here on purpose. An earlier version caught
        # KeyError and ValueError and re-raised them as TrackUnreadable, which
        # relabels a MALFORMED PAYLOAD -- a schema change in the runner, a
        # LanePayloadError, a LaneContractError, all of them ValueErrors -- as
        # "this file is unreadable". `analyze_one` turns that into a per-track
        # error and `run.py` into EXIT_TRACK_FAILURES, the one status a
        # chunking caller may continue past, so a systemic defect would be
        # rediscovered once per file across the whole library while reading as
        # a handful of bad tracks (Sol P1 BLOCKING, PR #1587). The per-FILE
        # cases are raised deliberately and by name: TrackVanished above,
        # TrackUnreadable inside record_from_payload for a runner-reported
        # error. Everything else propagates as the systemic fault it is.
        # ...and again afterwards, because "the file did not change while a
        # multi-second model ran over it" is exactly the kind of assumption
        # that holds until a sync tool, a re-tag or a re-encode lands in the
        # middle of a library-wide backfill. Cheap ways to ask (size, mtime)
        # answer a different question: a rewrite that preserves them is the
        # case worth catching, not the one worth excusing.
        if _fingerprint_or_unreadable(audio_path) != decode_fingerprint:
            raise TrackVanished(
                f"{audio_path} changed while the runner was analyzing it, so "
                "its beats came from bytes this record cannot vouch for"
            )
        try:
            return record_from_payload(
                payload,
                stable_id=stable_id,
                audio_path=audio_path,
                model_sha256=model_sha256,
                decode_fingerprint=decode_fingerprint,
                grid_fit=os.environ.get(GRID_FIT_ENV, "").strip() or GRID_FIT_RAW,
                genre=library_genre(stable_id),
            )
        except TrackUnreadable:
            # `record_from_payload` raises this specifically for a
            # RUNNER-REPORTED per-track error (`result["error"]`), a path that
            # bypasses `_fingerprint_or_unreadable` entirely - the recheck above
            # only catches a CONTENT disagreement against the fingerprint taken
            # before the runner ran, so a file that vanished for the runner's
            # own read and is present again (or byte-identical) by the time
            # that recheck runs still lands here unclassified. Left alone, this
            # reads as an ordinary per-track failure (`run.py`'s track-failure
            # status, not EXIT_MISSING_TARGETS), and a queue watcher marks the
            # target attempted rather than retrying it once the file returns
            # (Codex P2 BLOCKING, PR #1587). Rechecked here the same way
            # `_fingerprint_or_unreadable` already does for its own vanished
            # case.
            if not audio_path.exists():
                raise TrackVanished(
                    f"{audio_path} vanished while the runner was analyzing it"
                ) from None
            raise

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        # The model runs in a separate process with its own environment and
        # writes no numba cache this process shares. An empty tuple is the
        # contract's way of saying "I cannot vouch for one", which is true.
        return ()

    @classmethod
    def warm_jit_cache(cls) -> str:
        return (
            f"{BACKEND_NAME}: no JIT cache to warm (inference runs in the "
            "runner's own PEP 723 process)"
        )


register(OwnBeatgridBackfillBackend.name, OwnBeatgridBackfillBackend)

__all__ = [
    "BACKEND_NAME",
    "DEFAULT_DEVICE",
    "DEFAULT_TIMEOUT_S",
    "DEVICE_ENV",
    "RUNNER_PATH",
    "RUNNER_PYTHON_ENV",
    "TIMEOUT_ENV",
    "OwnBeatgridBackfillBackend",
    "RunnerPayloadError",
    "record_from_payload",
    "run_runner",
    "runner_command",
]
