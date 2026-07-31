"""Agent-native progress + efficiency reducer for the RoFormer Modal runner.

Sibling of ``scripts/farm_progress.py``, NOT a reuse of it -- both share the
same "one writer, one reducer, everybody reads the same numbers" philosophy,
but the transport and the derived stats differ enough that bolting this onto
farm_progress would mean gutting most of it.

WHY A SIBLING, NOT A REUSE:

  * Transport. ``farm_progress.ProgressLog`` appends to a LOCAL JSONL file,
    written by the DRIVER process as it observes ``modal_vocal_farm.py``'s
    ``starmap`` results streaming back. A driver only ever learns "this
    track finished or failed" that way -- ``starmap`` hands back one result
    per input, never a stream of partial updates -- so it can never report a
    track as mid-``separate()``. This runner's phases (loading / separating
    / encoding) happen INSIDE the remote H100 container, so only the
    container can report them, in real time, to something a wholly separate
    process can read without the driver still being alive or still holding
    the log file open. ``modal.Dict.from_name`` is exactly that: a KV store
    keyed by name, shared between local and remote processes regardless of
    which one currently has ``app.run()`` open -- see the writer half
    (``_progress_track`` et al.) in ``scripts/modal_roformer_spike.py``,
    mirrored there because the GPU container has no access to this file
    (same reason ``apps.stems.stem_size_policy`` is mirrored in that file
    for its output-format policy).
  * Stats. ``FarmState.eta_s`` is a flat completions/elapsed rate. This
    module's ETA is median per-track duration x remaining/concurrency
    (closer to true under bursty H100 pool concurrency), and the
    percentile/overhead stats below (``load_overhead_pct``, p50/p95
    ``sep_s`` per audio-minute) have no equivalent in ``FarmState`` at all --
    this runner's phase model (queued/loading/separating/encoding vs the
    farm's upload/gpu/stems/written async-transfer pipeline) is a different
    shape asking different questions.

What IS reused verbatim: run-id generation. ``farm_progress.new_run_id()``
is genuinely generic (a sortable timestamp+pid stamp), not vocal-farm-
specific, so ``modal_roformer_spike.py`` imports that function directly
rather than re-implementing it here.

Phase model per track (may fail at any phase):

    queued -> loading -> separating -> encoding -> done | failed

Every write is a WHOLE-STATE PUT keyed by ``stable_id``, not an append:
``{phase, ts, gpu_id, load_s, sep_s, encode_s, duration_s, error}``,
carrying forward everything known so far. A reader never needs history --
the latest write per track IS the current status -- which is what keeps
writes cheap (one ``modal.Dict`` RPC per transition, no read-modify-write,
no list append, no race: only ONE writer ever touches a given key, either
the driver before dispatch (``queued``) or the one container that owns that
track after dispatch).

This module owns the constants (source of truth) and the READER: fetching
the Dict's current contents, reducing them to counts, and deriving the
`status` JSON the runner's ``status`` subcommand prints. It has NO
top-level dependency on ``modal`` -- only ``read_state`` and
``containers_live_from_modal_cli`` touch Modal/the CLI, so every stats and
warnings function below is importable and unit-testable in the plain repo
venv, no ``uv run --with modal`` required (task requirement: pure math
covered by tests that need no Modal).

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 a status read is built from the Dict's CURRENT contents alone; a
    reader started after the run finished still sees the final, correct
    state (no history replay needed).
    [if] read_state runs after run_end [then] done+failed == total,
      in_flight == 0
  ✔︎ ✅ 🎯 an unknown run_id fails fast, not with an empty/zero snapshot.
    [if] the named Dict does not exist [then ⛔️] read_state raises SystemExit
  ✔︎ ✅ 🎯 percentile, ETA and overhead math never divide by zero and never
    fabricate a number before there is data to support it.
    [if] zero tracks are done yet [then] eta_s/p50/p95/load_overhead_pct
      all read 0.0, not NaN/inf
  ✔︎ ✅ 🎯 the three efficiency warnings fire on exactly their stated
    thresholds and nothing else.
    [if] load_overhead_pct > 30 [then] the warm-container warning appears
    [if] containers_live < min(10, remaining) and in_flight > 0 [then] the
      under-parallelised warning appears
    [if] p50 > 0 and p95/p50 > 3 [then] the straggler warning appears

-Claude
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

# ----- constants (source of truth; mirrored in modal_roformer_spike.py for
# the in-container writer -- see _assert_roformer_progress_mirror_matches
# there) ----------------------------------------------------------------------

DICT_PREFIX: str = "mdt-roformer-progress"
RUN_META_KEY: str = "__run__"

PHASES: frozenset[str] = frozenset(
    {"queued", "loading", "separating", "encoding", "done", "failed"}
)
ACTIVE_PHASES: frozenset[str] = frozenset({"loading", "separating", "encoding"})

# H100's published per-second Modal rate, same figure scripts/bench/
# ROFORMER-SPIKE.md's own cost table cites (and modal_vocal_farm.py's
# GPU_USD_PER_S["H100"] -- not imported from there since that file is WIP
# elsewhere and this runner never needs its other GPU tiers).
GPU_H100_USD_PER_S: float = 0.001097

# Fixed H100 pool concurrency cap from the task spec, independent of this
# runner's own --max-containers (DEFAULT_MAX_CONTAINERS): the warning is
# about the ACCOUNT-WIDE pool, not this run's own configured ceiling.
H100_POOL_CAP: int = 10

# Below this ratio a load_overhead_pct verdict has nothing to divide -- see
# load_overhead_pct's own zero-guard.
_MIN_DENOM: float = 1e-9


def dict_name(run_id: str) -> str:
    return f"{DICT_PREFIX}-{run_id}"


# ----- reading (touches Modal) ------------------------------------------------


def read_state(run_id: str) -> dict[str, dict[str, Any]]:
    """Every key currently in the run's Dict, meta entry included.

    Fails fast (SystemExit) on an unknown run_id -- a reader must never
    mistake "no such run" for "a run with zero progress yet".
    """
    import modal  # deferred: only this function needs the Modal client

    try:
        d = modal.Dict.from_name(dict_name(run_id), create_if_missing=False)
        return dict(d.items())
    except modal.exception.NotFoundError as exc:
        raise SystemExit(f"error: no progress found for run_id {run_id!r}: {exc}") from exc


def containers_live_from_modal_cli(app_id: str) -> int:
    """Live container (task) count for one app_id, via ``modal app list --json``.

    Scoped by app_id (captured at ``app.run()`` time and stored in the run's
    meta entry) rather than by app NAME, because two runs of this same
    script under the same app name can be live at once -- an app-name-only
    count would attribute another run's containers to this one.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "modal", "app", "list", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"modal app list --json failed rc={proc.returncode}: {proc.stderr}")
    apps = json.loads(proc.stdout)
    return containers_live_from_apps(apps, app_id)


def containers_live_from_apps(apps: list[dict[str, Any]], app_id: str) -> int:
    """Pure: sum the 'tasks' field for the app entry matching app_id.

    Unit-testable without the modal CLI by feeding a canned ``app list
    --json`` payload. Returns 0 for an app_id not (yet) present -- a run
    whose meta write landed but whose app.run() has not registered yet is
    "zero live containers", not an error.
    """
    total = 0
    for entry in apps:
        if entry.get("app_id") == app_id:
            total += int(entry.get("tasks", 0) or 0)
    return total


# ----- reducing (pure) ---------------------------------------------------------


@dataclass
class RunSnapshot:
    run_id: str
    total: int
    config_tag: str
    checkpoint: str
    max_containers: int
    app_id: str
    started_ts: float
    tracks: dict[str, dict[str, Any]] = field(default_factory=dict)


def reduce_state(raw: dict[str, dict[str, Any]]) -> RunSnapshot:
    """Split the raw Dict contents into the run meta entry and per-track
    latest-phase entries. Pure -- takes whatever read_state returned (or a
    hand-built dict in a test), touches nothing external."""
    meta = raw.get(RUN_META_KEY)
    if meta is None:
        raise ValueError(
            f"progress state has no {RUN_META_KEY!r} meta entry -- run_start "
            "was never written for this run_id"
        )
    tracks = {key: value for key, value in raw.items() if key != RUN_META_KEY}
    return RunSnapshot(
        run_id=meta.get("run_id", ""),
        total=meta.get("total", 0),
        config_tag=meta.get("config_tag", ""),
        checkpoint=meta.get("checkpoint", ""),
        max_containers=meta.get("max_containers", 0),
        app_id=meta.get("app_id", ""),
        started_ts=meta.get("started_ts", 0.0),
        tracks=tracks,
    )


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (numpy's default 'linear' method),
    picked over statistics.quantiles for one reason: p50 and p95 are only
    meaningfully comparable (the straggler-ratio warning divides one by the
    other) if both come from the exact same interpolation rule."""
    if not 0 <= pct <= 100:
        raise ValueError(f"pct must be within 0..100, got {pct}")
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100)
    lower, upper = math.floor(rank), math.ceil(rank)
    if lower == upper:
        return ordered[int(rank)]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def done_count(snapshot: RunSnapshot) -> int:
    return sum(1 for t in snapshot.tracks.values() if t.get("phase") == "done")


def failed_count(snapshot: RunSnapshot) -> int:
    return sum(1 for t in snapshot.tracks.values() if t.get("phase") == "failed")


def in_flight_count(snapshot: RunSnapshot) -> int:
    return sum(1 for t in snapshot.tracks.values() if t.get("phase") in ACTIVE_PHASES)


def settled_count(snapshot: RunSnapshot) -> int:
    """Tracks that will not move again: done, or failed anywhere."""
    return done_count(snapshot) + failed_count(snapshot)


def pct_complete(snapshot: RunSnapshot) -> float:
    if snapshot.total <= 0:
        return 0.0
    return round(100.0 * settled_count(snapshot) / snapshot.total, 1)


def _done_timings(snapshot: RunSnapshot) -> list[dict[str, Any]]:
    return [t for t in snapshot.tracks.values() if t.get("phase") == "done"]


def eta_s(snapshot: RunSnapshot, containers_live: int) -> float:
    """Median-based ETA: median per-track wall time (load+sep+encode) among
    DONE tracks, times remaining tracks divided by live concurrency.

    Median rather than mean because one slow track (a long remix) must not
    drag the estimate for every other track the way an average would; see
    the straggler warning below for the companion diagnostic. Concurrency
    prefers the run's OWN live container count (containers_live, app_id-
    scoped) and only falls back to the configured max_containers before any
    container has registered, so an early poll does not read as "serial".
    """
    remaining = snapshot.total - settled_count(snapshot)
    if remaining <= 0:
        return 0.0
    durations = [
        (t.get("load_s") or 0.0) + (t.get("sep_s") or 0.0) + (t.get("encode_s") or 0.0)
        for t in _done_timings(snapshot)
    ]
    if not durations:
        return 0.0
    median_track_s = _percentile(durations, 50)
    concurrency = containers_live if containers_live > 0 else max(1, snapshot.max_containers)
    return round(remaining / concurrency * median_track_s, 1)


def gpu_s_spent(snapshot: RunSnapshot) -> float:
    """Container-active seconds, summed across DONE tracks (load_s + sep_s +
    encode_s). Same lower-bound proxy scripts/bench/ROFORMER-SPIKE.md's own
    cost table uses when the full separate_track wrapper timing (gpu_s) is
    not directly available to a reader -- see its 'Library render' cost row
    for the precedent."""
    return round(
        sum(
            (t.get("load_s") or 0.0) + (t.get("sep_s") or 0.0) + (t.get("encode_s") or 0.0)
            for t in _done_timings(snapshot)
        ),
        2,
    )


def usd_spent(gpu_seconds: float) -> float:
    return round(gpu_seconds * GPU_H100_USD_PER_S, 4)


def load_overhead_pct(snapshot: RunSnapshot) -> float:
    """sum(load_s) / sum(load_s + sep_s) across DONE tracks, as a percent.
    Exact formula the task spec gives -- deliberately excludes encode_s: the
    question this answers is "how much of GPU-bound time is spent loading
    the checkpoint vs actually separating", and encoding is not GPU-bound."""
    done = _done_timings(snapshot)
    load_sum = sum((t.get("load_s") or 0.0) for t in done)
    sep_sum = sum((t.get("sep_s") or 0.0) for t in done)
    denom = load_sum + sep_sum
    if denom < _MIN_DENOM:
        return 0.0
    return round(100.0 * load_sum / denom, 1)


def _sep_s_per_audio_min_values(snapshot: RunSnapshot) -> list[float]:
    values: list[float] = []
    for t in _done_timings(snapshot):
        sep_s = t.get("sep_s")
        duration_s = t.get("duration_s")
        if not sep_s or not duration_s:
            continue
        values.append(sep_s / (duration_s / 60.0))
    return values


def p50_sep_s_per_audio_min(snapshot: RunSnapshot) -> float:
    values = _sep_s_per_audio_min_values(snapshot)
    return round(_percentile(values, 50), 2) if values else 0.0


def p95_sep_s_per_audio_min(snapshot: RunSnapshot) -> float:
    values = _sep_s_per_audio_min_values(snapshot)
    return round(_percentile(values, 95), 2) if values else 0.0


# ----- warnings (pure) ----------------------------------------------------------


def build_warnings(
    *, load_overhead: float, containers_live: int, remaining: int, in_flight: int,
    p50: float, p95: float,
) -> list[str]:
    """Exactly the three rules the task spec gives. Nothing speculative."""
    warnings: list[str] = []
    if load_overhead > 30:
        warnings.append(
            "model load dominating, use warm containers (modal.Cls) or memory snapshots"
        )
    if containers_live < min(H100_POOL_CAP, remaining) and in_flight > 0:
        warnings.append("under-parallelised vs 10-GPU concurrency cap")
    if p50 > 0 and (p95 / p50) > 3:
        warnings.append("straggler tracks, investigate longest audio")
    return warnings


# ----- status JSON (pure, given a snapshot + a containers_live count) ----------


def build_status(snapshot: RunSnapshot, *, containers_live: int) -> dict[str, Any]:
    """The single JSON object the runner's `status` subcommand prints. Pure:
    everything here is arithmetic over already-fetched data, so this (and
    everything it calls) is covered by tests with no Modal dependency."""
    done = done_count(snapshot)
    failed = failed_count(snapshot)
    in_flight = in_flight_count(snapshot)
    remaining = snapshot.total - settled_count(snapshot)
    gpu_s = gpu_s_spent(snapshot)
    load_overhead = load_overhead_pct(snapshot)
    p50 = p50_sep_s_per_audio_min(snapshot)
    p95 = p95_sep_s_per_audio_min(snapshot)
    return {
        # run_id is additive context, not one of the required fields below.
        "run_id": snapshot.run_id,
        "done": done,
        "total": snapshot.total,
        "pct": pct_complete(snapshot),
        "in_flight": in_flight,
        "failed": failed,
        "eta_s": eta_s(snapshot, containers_live),
        "gpu_s_spent": gpu_s,
        "usd_spent": usd_spent(gpu_s),
        "containers_live": containers_live,
        "load_overhead_pct": load_overhead,
        "p50_sep_s_per_audio_min": p50,
        "p95_sep_s_per_audio_min": p95,
        "warnings": build_warnings(
            load_overhead=load_overhead,
            containers_live=containers_live,
            remaining=remaining,
            in_flight=in_flight,
            p50=p50,
            p95=p95,
        ),
    }
