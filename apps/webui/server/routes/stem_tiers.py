"""Separation-tier catalogue and per-track time/cost estimates.

Read-only. Runs nothing, spends nothing, and never guesses: every number comes
from :mod:`apps.stems.tiers`, which will raise rather than interpolate an
estimate for a (tier, GPU) pair that has not been benchmarked.

Mounted at ``/api/v1`` by the application integrator.

  GET  /api/v1/stems/tiers                  the ladder and its evidence
  GET  /api/v1/stems/estimate?seconds=240   every rung costed for one track
  GET  /api/v1/stems/estimate/batch?...     wall clock for a whole batch
  POST /api/v1/stems/generate               start one real separation
  GET  /api/v1/stems/jobs/{id}              live state of a generate job

AGENT-NATIVE PARITY: every control the right-click menu offers is reachable
here, and the estimate endpoints mirror ``python -m apps.stems estimate``
exactly, so an agent can drive the identical flow without a browser.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from apps.stems import tiers as tiercfg
from apps.webui.server.routes.stems_parity_guard import guard_stems_parity_call

router = APIRouter(prefix="/stems", tags=["stem-tiers"])


class TierOut(BaseModel):
    """One separation product, with the evidence behind the choice."""

    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    where: str
    preset_tag: str
    model: str
    overlap: float
    shifts: int
    gpu: str
    #: Stem container this rung emits. Exposed because it is the single biggest
    #: driver of what a caller downloads: L is flac at ~3.7x the bytes of the
    #: opus rungs (53.17 MB vs 14.45 MB per 2.50-min track, measured), so a UI
    #: that cannot see it cannot warn before a bulk run.
    codec: str
    purpose: str
    evidence: str
    evidence_strength: str
    availability: str
    unavailable_because: str
    is_default: bool
    #: False when THIS engine refuses to spawn the tier's process (INSTALL-32):
    #: a Modal tier in the installed app, which ships no uv and no modal. The
    #: UI hides such a tier; ``generate`` refuses it with the same sentence.
    runnable_here: bool
    not_runnable_because: str | None = None


class TierEstimateOut(BaseModel):
    """Cost of one track at one tier. ``measured`` false means we do not know."""

    model_config = ConfigDict(frozen=True)

    tier: str
    name: str
    where: str
    gpu: str
    availability: str
    unavailable_because: str
    measured: bool
    seconds: float | None = None
    usd: float | None = None
    measured_at: str | None = None
    n_tracks: int | None = None
    r_squared: float | None = None
    unavailable_reason: str | None = None


class EstimateOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    duration_s: float
    tiers: list[TierEstimateOut]


def _ensure_loaded() -> None:
    """Load the measured fits once. Cheap, idempotent, and explicit."""
    if not tiercfg.THROUGHPUT:
        from apps.shared.paths import DATA_DIR

        tiercfg.load_measured_throughput(DATA_DIR.parent)


def _spawn_refusal(tier: tiercfg.Tier) -> str | None:
    """Why this engine cannot spawn ``tier``'s process, or None if it can.

    One predicate for both ``GET /tiers`` (the UI hides a refused tier) and
    ``_generate_command`` (the 503 backstop), so the two cannot disagree. A
    Modal tier runs scripts/modal_vocal_farm.py through ``uv --with modal``;
    the installed app ships none of the three (INSTALL-31).
    """
    if tier.where == "local":
        return None
    from apps.shared.source_tree import is_repo_checkout
    from apps.stems.worker_launch import packaged_python

    if packaged_python() or not is_repo_checkout(_repo_root()):
        return (
            f"tier {tier.key} separates on Modal through uv, which needs a "
            "development checkout with uv and modal; this installed app "
            "ships neither. Choose a local tier, or run the engine from a "
            "checkout of the repo."
        )
    return None


@router.get("/tiers", response_model=list[TierOut])
def list_tiers() -> list[TierOut]:
    return [
        _tier_out(t)
        for t in tiercfg.ladder()  # ladder order, NOT_APPLICABLE rungs included
    ]


def _tier_out(t: tiercfg.Tier) -> TierOut:
    refusal = _spawn_refusal(t)
    return TierOut(
        key=t.key,
        name=t.name,
        where=t.where,
        preset_tag=t.preset_tag,
        model=t.model,
        overlap=t.overlap,
        shifts=t.shifts,
        gpu=t.gpu,
        codec=t.codec,
        purpose=t.purpose,
        evidence=t.evidence,
        evidence_strength=t.evidence_strength,
        availability=t.availability,
        unavailable_because=t.unavailable_because,
        is_default=(t.key == tiercfg.DEFAULT_TIER),
        runnable_here=refusal is None,
        not_runnable_because=refusal,
    )


@router.get("/estimate", response_model=EstimateOut)
def estimate(
    seconds: float = Query(..., gt=0, description="track duration in seconds"),
    gpu: str | None = Query(None, description="override the card"),
) -> EstimateOut:
    """Every tier costed for one track, so a UI needs one round trip not three.

    A tier with no benchmark returns ``measured: false`` and the reason rather
    than a plausible-looking guess. Rendering a guess as a number is how an
    assumption becomes ground truth.
    """
    return guard_stems_parity_call(lambda: _estimate_impl(seconds, gpu))


def _estimate_impl(seconds: float, gpu: str | None) -> EstimateOut:
    _ensure_loaded()
    out: list[TierEstimateOut] = []
    for tier in tiercfg.ladder():
        card = gpu or tier.gpu
        try:
            secs = tiercfg.estimate_seconds(seconds, tier.key, card)
            usd = tiercfg.estimate_usd(seconds, tier.key, card)
            tp = tiercfg.THROUGHPUT[f"{tier.key}@{card}"]
            out.append(TierEstimateOut(
                tier=tier.key, name=tier.name, where=tier.where, gpu=card,
                availability=tier.availability,
                unavailable_because=tier.unavailable_because, measured=True,
                seconds=round(secs, 1), usd=round(usd, 4),
                measured_at=tp.measured_at, n_tracks=tp.n_tracks,
                r_squared=tp.r_squared,
            ))
        except tiercfg.ThroughputNotMeasured as exc:
            out.append(TierEstimateOut(
                tier=tier.key, name=tier.name, where=tier.where, gpu=card,
                availability=tier.availability,
                unavailable_because=tier.unavailable_because, measured=False,
                unavailable_reason=str(exc),
            ))
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return EstimateOut(duration_s=seconds, tiers=out)


class GenerateIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    tier: str


# In-process job table. Deliberately NOT persisted: a job whose process died
# with the server is not a job, and pretending otherwise on restart would be
# exactly the invented state this codebase forbids.
_JOBS: dict[str, dict] = {}

# CONCURRENCY CAPS. This endpoint is live and agent-drivable, so "how many at
# once" is a real question with two different answers.
#
# Modal rungs are capped at the plan's GPU allowance: going past it does not
# fail, it queues, and a queued container still bills once it starts. The cap
# makes the ceiling visible instead of discovering it on an invoice.
#
# LOCAL is capped at ONE. Each local job spawns a torch process on the maintainer's Mac;
# two of those is not twice as fast, it is one slow machine, and this endpoint
# has no idea what else is running.
_MAX_LOCAL_JOBS: int = 1
# Keep finished jobs around so a caller that polls late still gets the real
# returncode, but bound the table so a long-lived server cannot grow forever.
_MAX_JOB_HISTORY: int = 200


def _reap_jobs() -> None:
    """Drop the oldest FINISHED jobs once the table is over its bound.

    Running jobs are never dropped: losing the handle would orphan a live
    subprocess and make its status unknowable.
    """
    if len(_JOBS) <= _MAX_JOB_HISTORY:
        return
    finished = [k for k, j in _JOBS.items() if j["proc"].poll() is not None]
    for key in finished[: len(_JOBS) - _MAX_JOB_HISTORY]:
        _JOBS.pop(key, None)


def _running_count(where: str) -> int:
    return sum(
        1
        for j in _JOBS.values()
        if j["where"] == where and j["proc"].poll() is None
    )


def _repo_root() -> Path:
    """The source tree: read-only in the installed app (``payload/app`` inside
    the signed bundle). Workers are FOUND here; nothing is WRITTEN here."""
    return Path(__file__).resolve().parents[4]


JOB_LOG_SUBDIR: str = "logs/stem-jobs"


def _job_log_dir() -> Path:
    """Where a generate job's log goes: the data dir's ``logs/``, the same
    writable, per-engine place as the engine's own logs (``EngineConfig``'s
    ``logs_dir``), never the source tree.

    It used to be ``_repo_root() / ".tmp/stem-jobs"``. In the installed app
    that is inside ``Open DJ.app``, so one LOCAL job added a file to the
    sealed bundle and ``codesign --verify --deep --strict`` failed with "a
    sealed resource is missing or invalid" (packaged check of 316572f5,
    Fri 2 Oct 2026, finding 2; STEM-49). Read at call time so a test that
    repoints the data dir measures it.
    """
    from apps.shared import paths

    return Path(paths.DATA_DIR) / JOB_LOG_SUBDIR


def _generate_command(
    tier, stable_id: str, audio_path: str, *, stems_dir: Path
) -> list[str]:
    """The exact argv for this tier. One place, so CLI and UI cannot diverge."""
    root = _repo_root()
    if tier.where == "local":
        # The installed app ships no uv; its launcher names the payload
        # interpreter instead (apps/stems/worker_launch.py, issue #3421).
        from apps.stems.worker_launch import packaged_python

        packaged = packaged_python()
        prefix = [packaged] if packaged else ["uv", "run", "--no-sync"]
        return [
            *prefix, str(root / "scripts/stem_bundle_worker.py"),
            "--audio", audio_path,
            "--stable-id", stable_id,
            "--out-dir", str(stems_dir / stable_id),
        ]
    # `uv run --no-sync` outside a project dies with an opaque error, so the
    # installed app refuses before spawning (INSTALL-31); the UI already hides
    # the tier from the same predicate (INSTALL-32).
    refusal = _spawn_refusal(tier)
    if refusal is not None:
        from apps.shared.source_tree import DEV_ONLY_CODE

        raise HTTPException(
            status_code=503, detail={"code": DEV_ONLY_CODE, "message": refusal}
        )
    return [
        "uv", "run", "--no-sync", "--with", "modal", "python", "-m",
        "scripts.modal_vocal_farm",
        "--tier", tier.key,
        "--only-stable-id", stable_id,
    ]


@router.post("/generate", response_model=dict)
def generate(body: GenerateIn, request: Request) -> dict:
    """Start a real separation for one track at one rung. No mocking.

    Returns immediately with a job id; the work runs as a subprocess. This is
    the programmatic half of the right-click menu item, so an agent can drive
    the identical flow (AGENT-NATIVE PARITY).
    """
    return guard_stems_parity_call(lambda: _generate_impl(body, request))


def _generate_impl(body: GenerateIn, request: Request) -> dict:
    import subprocess
    import uuid

    _ensure_loaded()
    try:
        tier = tiercfg.get_tier(body.tier)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if tier.availability != "AVAILABLE":
        raise HTTPException(
            status_code=409,
            detail=(
                f"tier {tier.key} is {tier.availability}: "
                f"{tier.unavailable_because}"
            ),
        )

    from apps.shared.paths import DATA_DIR
    from apps.stems.cli import resolve_audio_path

    try:
        audio_path = resolve_audio_path(DATA_DIR, body.stable_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    _reap_jobs()
    limit = _MAX_LOCAL_JOBS if tier.where == "local" else tiercfg.MAX_CONCURRENT_GPUS
    running = _running_count(tier.where)
    if running >= limit:
        raise HTTPException(
            status_code=429,
            detail=(
                f"{running} {tier.where} separation job(s) already running, "
                f"limit {limit}. "
                + (
                    "Local jobs each spawn a torch process on this Mac; two at "
                    "once is one slow machine, not twice the throughput."
                    if tier.where == "local"
                    else "Past the plan's GPU allowance Modal queues rather "
                    "than fails, and a queued container still bills."
                )
            ),
        )

    from apps.webui.library_assets import (
        StemStorage,
        ensure_stem_storage,
        stem_storage,
    )

    configured_roots = getattr(request.app.state, "stem_roots", None)
    storage = stem_storage()
    if configured_roots is not None:
        roots = tuple(Path(root) for root in configured_roots)
        storage = StemStorage(
            roots=roots,
            write_root=roots[0],
            remote=storage.remote,
        )
    ensure_stem_storage(storage)
    cmd = _generate_command(
        tier,
        body.stable_id,
        str(audio_path),
        stems_dir=storage.write_root,
    )
    job_id = uuid.uuid4().hex[:12]
    log = _job_log_dir() / f"{job_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    # The parent closes its copy as soon as the child owns one: leaving it open
    # leaks a descriptor per job, and job_status reads the file by path anyway.
    with log.open("w") as handle:
        proc = subprocess.Popen(
            cmd, cwd=str(_repo_root()), stdout=handle, stderr=subprocess.STDOUT
        )
    _JOBS[job_id] = {
        "job_id": job_id,
        "stable_id": body.stable_id,
        "tier": tier.key,
        "where": tier.where,
        "command": cmd,
        "log": str(log),
        "proc": proc,
    }
    return {
        "job_id": job_id,
        "tier": tier.key,
        "stable_id": body.stable_id,
        "command": " ".join(cmd),
        "log": str(log),
        "poll": f"/api/v1/stems/jobs/{job_id}",
    }


@router.get("/jobs/{job_id}", response_model=dict)
def job_status(job_id: str) -> dict:
    """Live state of one generate job. Reports the real returncode, not a guess."""
    from pathlib import Path

    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
    rc = job["proc"].poll()
    if rc is None:
        state = "running"
    elif rc == 0:
        state = "done"
    else:
        state = "failed"
    tail = ""
    log_path = Path(job["log"])
    if log_path.exists():
        tail = log_path.read_text(errors="replace")[-4000:]
    return {
        "job_id": job_id,
        "stable_id": job["stable_id"],
        "tier": job["tier"],
        "state": state,
        "returncode": rc,
        "command": " ".join(job["command"]),
        "log_tail": tail,
    }


@router.get("/estimate/batch", response_model=dict)
def estimate_batch(
    seconds: list[float] = Query(..., description="repeat once per track"),  # noqa: B008  # FastAPI DI
    tier: str = Query(tiercfg.DEFAULT_TIER),
    gpu: str | None = Query(None),
) -> dict:
    """Wall clock for a whole batch, packed longest-first across GPU slots.

    GPU-SIDE ONLY. It does not model the upload feeder, which has been the
    binding constraint on real runs from this Mac. A floor, not a promise.
    """
    _ensure_loaded()
    try:
        total = tiercfg.estimate_batch_seconds(list(seconds), tier, gpu)
    except tiercfg.ThroughputNotMeasured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "tier": tier.upper(),
        "gpu": gpu or tiercfg.get_tier(tier).gpu,
        "n_tracks": len(seconds),
        "total_audio_s": round(sum(seconds), 1),
        "max_concurrent_gpus": tiercfg.MAX_CONCURRENT_GPUS,
        "wall_s": round(total, 1),
        "wall_human": f"{int(total // 60)}m {int(total % 60)}s",
        "caveat": (
            "GPU-side only; excludes the local upload feeder, which has been "
            "the real binding constraint. Treat as a floor."
        ),
    }
