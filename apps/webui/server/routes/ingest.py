"""Ingestion pipeline endpoints: config, coverage, refresh job, upload+dedup.

Backs two UI features and their agent-native parity:

  * "Refresh analysis" TopBar button  -> GET /ingest/coverage,
    POST /ingest/refresh, GET /ingest/refresh/status
  * Analyze-on-import for local tracks -> POST /ingest/refresh
    ``{"scope": "unmapped"}``; see routes/analysis_queue.py
  * Drag-in ingest modal              -> GET/PUT /ingest/config,
    POST /ingest/upload (moved to routes/ingest_upload.py; shared CFG here)

Design constraints honoured here:
  * Rekordbox remains the only writer of its own DB; upload stages files
    under ``INGEST_INBOX`` (``~/Music/Manual Library/_ingest/<batch>/`` when
    ``MDT_DATA_DIR`` is unset; ``<data_dir>/Manual Library/_ingest/<batch>/``
    when it is set) and RB import stays a human step (see
    .agents/skills/ingest-new-tracks).
  * The refresh job shells out to the existing CLIs (apps.analysis.run,
    apps.stems trickle, apps.vocals from-stems) rather than reimplementing
    them - one code path per pipeline, real data only.
  * Duplicate policy at the door: an uploaded file whose chromaprint
    similarity to an existing library track is >= DUP_FP_THRESHOLD is NOT
    staged (skipped with the existing stable_id reported) unless the client
    passes ``force``. One track, one set of metadata.
  * Fail fast: unknown step ids 422, second concurrent refresh 409, missing
    fpcalc reports method="duration" explicitly - never silently.

Requirements (mini-PRD):
  ✔︎ ✅ GET/PUT config: persisted checkbox list driving both the modal and
    the refresh job.
    [if] PUT contains an unknown step id [then ⛔️] 422, nothing persisted
    [if] config file absent [then] code DEFAULT_STEPS returned and persisted
  ✔︎ ✅ GET coverage: per-step missing counts from real artifacts
    (analysis table, stems bundles, vocal-cache, lyrics-cache) over on-disk
    tracks. lyrics is coverage-only - it has no STEPS/refresh runner.
    [if] a track's file is a broken link [then] it is excluded and counted
    in ``unreachable`` instead of any step's missing list
  ✔︎ ✅ POST refresh + status: background job over missing tracks for
    enabled steps; ring-buffer log; progress per step.
    [if] a refresh is already running [then ⛔️] 409
    [if] a step subprocess exits non-zero [then] job phase "error" with the
    tail of its output, later steps not run
  ✔︎ ✅ scope="unmapped": the same job restricted to tracks that landed with
    no rekordbox mapping, so local-first tracks get analysis rows to serve.
    [if] it carries a batch_dir, or runs with analysis disabled [then ⛔️] 422
    [if] a target is already analyzed or rekordbox-mapped [then] it is skipped
  ✔︎ ✅ POST upload: moved to routes/ingest_upload.py with its own mini-PRD.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel

from apps.analysis import backlog
from apps.analysis import run as analysis_run
from apps.shared.events import publish
from apps.shared.paths import AUDIO_EXTENSIONS, INGEST_INBOX, STATE_DB
from apps.shared.state.db import open_ro
from apps.stems.artifacts import DEFAULT_STEMS_DIR, stem_roots
from apps.webui.server import coverage_cloud
from apps.webui.server.routes import ingest_coverage
from apps.webui.server.routes.ingest_analysis_argv import CliFailed, build_analysis_argv
from apps.webui.server.routes.ingest_job import (
    _JOBS,
    _PER_TARGET_EXITS,
    ACTIVE_PHASES,
    UNMAPPED_SCOPE,
    RefreshStatusOut,
    _job_lock,
    _log,
    _RefreshJob,
    _run_cli,
    _systemic_message,
    missing_by_step,
    tracks_on_disk,
)
from apps.webui.server.routes.ingest_scope import RefreshIn, resolve_scope, unmapped_steps
from apps.webui.server.routes.ingest_track import select_track_target

router = APIRouter(prefix="/ingest", tags=["ingest"])


def _stem_roots(app: FastAPI) -> tuple[Path, ...]:
    """The app's configured stem roots (remote library mode), else the local
    default farm/RoFormer roots. Coverage, refresh targeting and the deck all
    have to resolve roots through this one path, or a mode where the app
    configures crate-backed roots sees a different set than it was told."""
    configured = getattr(app.state, "stem_roots", None)
    if configured is not None:
        return tuple(Path(root) for root in configured)
    return stem_roots(DEFAULT_STEMS_DIR)


#: Lines of the job log the status response carries. 60 lost the head of a
#: faulthandler dump (about 40 lines: the fatal-signal line, one stack per
#: thread, the extension-module list) behind the two lines the drain appends
#: after a crashed chunk, so the CI log showed the crash's outermost frames and
#: not the one that faulted (e2e run 34339762348, Wed 9 Sep 2026).
LOG_TAIL_LINES: int = 200

# ----- CFG -------------------------------------------------------------------
CONFIG_PATH: Path = STATE_DB.parent / "ingest-config.json"
VOCAL_CACHE_DIR: Path = STATE_DB.parent / "vocal-cache"
LYRICS_CACHE_DIR: Path = STATE_DB.parent / "lyrics-cache"
#: Data dir the lyrics-fetch verdicts and the coverage outcome ledger live under.
COVERAGE_DATA_DIR: Path = STATE_DB.parent.parent
DUP_DURATION_TOLERANCE_MS: int = 1_500
DUP_FP_THRESHOLD: float = 0.92          # matches apps.dedup.find_clusters
DUP_MAX_FP_CANDIDATES: int = 5          # fingerprinting candidates is O(seconds) each
ANALYSIS_CHUNK: int = 25                # progress granularity for the analysis step
#: Backend every drain chunk runs. Named here rather than left to the CLI's
#: own default so the drain and the backlog's ``analyzed`` bucket, which is
#: scoped to one backend, cannot drift apart.
ANALYSIS_BACKEND: str = backlog.DRAIN_BACKEND
STEMS_TRICKLE_LIMIT: int = 5
BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,79}$")

# Registry of pipeline steps. ``requires_rb_row`` steps need the track in
# Rekordbox + state.db first (stems/vocals are keyed by stable_id), so the
# drag-in modal greys them out until the batch has been imported into RB.
STEPS: list[dict] = [
    {
        "id": "analysis",
        "label": "Analysis (BPM / key / energy)",
        "default_enabled": True,
        "requires_rb_row": False,
        "hint": "librosa+madmom via apps.analysis.run; idempotent per file.",
    },
    {
        "id": "stems",
        "label": f"Stems (local trickle, max {STEMS_TRICKLE_LIMIT}/run)",
        "default_enabled": False,
        "requires_rb_row": True,
        "hint": "~104MB per bundle - bulk runs belong on the Modal farm.",
    },
    {
        "id": "vocals",
        "label": "Vocals (from existing stems)",
        "default_enabled": True,
        "requires_rb_row": True,
        "hint": "apps.vocals from-stems --live; CPU, no demucs.",
    },
]
_STEP_IDS: frozenset[str] = frozenset(s["id"] for s in STEPS)


# ----- config ---------------------------------------------------------------
class ConfigOut(BaseModel):
    steps: list[dict]
    path: str


class ConfigIn(BaseModel):
    enabled: dict[str, bool]


def _load_enabled() -> dict[str, bool]:
    if CONFIG_PATH.exists():
        stored = json.loads(CONFIG_PATH.read_text())["enabled"]
        unknown = set(stored) - _STEP_IDS
        if unknown:
            raise HTTPException(
                500,
                f"{CONFIG_PATH} contains unknown step ids {sorted(unknown)}; "
                "fix or delete the file",
            )
        return {s["id"]: stored.get(s["id"], s["default_enabled"]) for s in STEPS}
    return {s["id"]: s["default_enabled"] for s in STEPS}


def _persist_enabled(enabled: dict[str, bool]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"enabled": enabled}, indent=1))
    tmp.rename(CONFIG_PATH)


@router.get("/config", response_model=ConfigOut)
def get_config() -> ConfigOut:
    enabled = _load_enabled()
    if not CONFIG_PATH.exists():
        _persist_enabled(enabled)
    return ConfigOut(
        steps=[{**s, "enabled": enabled[s["id"]]} for s in STEPS],
        path=str(CONFIG_PATH),
    )


@router.put("/config", response_model=ConfigOut)
def put_config(body: ConfigIn) -> ConfigOut:
    unknown = set(body.enabled) - _STEP_IDS
    if unknown:
        raise HTTPException(422, f"unknown step ids: {sorted(unknown)}")
    enabled = {**_load_enabled(), **body.enabled}
    _persist_enabled(enabled)
    return get_config()


# ----- coverage -------------------------------------------------------------
class CoverageOut(BaseModel):
    """Per-step coverage over ``present`` tracks; see routes/ingest_coverage.py.

    ``on_disk`` is the denominator (``availability.present``). Per step,
    ``done + terminal + failed + pending == on_disk``. ``missing`` and
    ``corrupt`` keep their artifact meaning for the refresh job's targeting:
    ``corrupt`` (structurally invalid entries) is a subset of ``missing``.
    """

    total_tracks: int
    on_disk: int
    unreachable: int
    missing: dict[str, int]
    corrupt: dict[str, int]
    availability: dict[str, int]
    done: dict[str, int]
    terminal: dict[str, int]
    failed: dict[str, int]
    pending: dict[str, int]
    waiting_on_stems: int
    #: Stems ``done`` split (HEALTH-07): on this disk, and only in R2.
    local: dict[str, int]
    in_cloud: dict[str, int]
    #: Vocals that need their bundle fetched from R2 before they can derive.
    awaiting_stem_download: int
    #: ``state`` is ok | off | unknown; unknown renders the stems light grey.
    stems_index: dict[str, str | None]
    stems_source_refusal: str | None
    generated_at: float


def build_snapshot(app: FastAPI) -> ingest_coverage.CoverageSnapshot:
    """The one coverage measurement, shared by the route and the auto-drain."""
    refusal_fn = getattr(
        app.state, "stems_source_refusal_fn", ingest_coverage.default_stems_source_refusal
    )
    return ingest_coverage.compute_snapshot(
        open_ro, _stem_roots(app), VOCAL_CACHE_DIR, LYRICS_CACHE_DIR, COVERAGE_DATA_DIR,
        stems_source_refusal=refusal_fn(),
        stem_cloud=coverage_cloud.for_app_state(app.state, COVERAGE_DATA_DIR),
    )


@router.get("/coverage", response_model=CoverageOut)
def get_coverage(request: Request) -> CoverageOut:
    snapshot = build_snapshot(request.app)
    return CoverageOut.model_validate(
        {
            "total_tracks": snapshot.playability.total,
            **ingest_coverage.response_fields(snapshot),
        }
    )


# ----- refresh job ----------------------------------------------------------
def _run_analysis_chunk(
    job: _RefreshJob, chunk: list[tuple[str, str]], *, backend: str = ANALYSIS_BACKEND
) -> None:
    """Run one chunk of (stable_id, path) targets through apps.analysis.run.

    ``backend`` is a parameter for the reason :func:`build_analysis_argv`
    takes one: a caller can name a backend this machine genuinely cannot
    run, which is how the capability-failure path gets exercised without
    replacing this module's production choice underneath it.
    """
    argv, pairs_path = build_analysis_argv(chunk, backend)
    try:
        _run_cli(job, argv)
    finally:
        if pairs_path is not None:
            Path(pairs_path).unlink(missing_ok=True)


def _step_analysis(
    job: _RefreshJob, targets: list[tuple[str, str]], *, backend: str = ANALYSIS_BACKEND
) -> None:
    """Give every chunk its turn, then fail if any of them did.

    Aborting on the first non-zero chunk would starve every valid track
    sorted behind a few permanently unanalyzable ones: nothing later runs, no
    analysis rows appear, the backlog signature never moves, and the
    reconcile loop reads that as "already tried this queue". Failures are
    accumulated, never swallowed - the step still raises at the end.

    Only a PER-TARGET exit earns that, which is why the CLI names its exit
    codes. Anything else - no usable backend on this machine, an argparse or
    configuration error, a signal, a crashed worker pool, a SQLITE_FULL on
    the state DB - recurs identically on the next chunk, so continuing means
    decoding the whole library to meet the same wall once per chunk.
    """
    job.step_total = len(targets)
    job.step_done = 0
    _log(job, f"analysis: {len(targets)} tracks missing")
    failures: list[str] = []
    systemic: CliFailed | None = None
    for i in range(0, len(targets), ANALYSIS_CHUNK):
        chunk = targets[i : i + ANALYSIS_CHUNK]
        try:
            _run_analysis_chunk(job, chunk, backend=backend)
        except CliFailed as exc:
            failures.append(f"chunk at {i}: {exc}")
            if exc.returncode == analysis_run.EXIT_MISSING_TARGETS:
                # Targets vanished between the scan and the CLI's own check,
                # so this queue was never really attempted. Publish nothing:
                # the next tick has to retry it, not book it as tried.
                job.queue_signature = None
            if exc.returncode not in _PER_TARGET_EXITS:
                _log(job, f"analysis: chunk at {i} failed systemically; stopping")
                systemic = exc
                job.step_done += len(chunk)
                break
            _log(job, f"analysis: chunk at {i} failed ({exc}); continuing")
        else:
            for sid, _ in chunk:
                job.recently_done_ids.append(sid)
        job.step_done += len(chunk)
    if systemic is not None:
        raise RuntimeError(_systemic_message(systemic, len(targets), backend))
    if failures:
        raise RuntimeError(
            f"analysis failed on {len(failures)} chunk(s): " + "; ".join(failures)
        )


def _step_stems(job: _RefreshJob, targets: list[tuple[str, str]]) -> None:
    job.step_total = min(len(targets), STEMS_TRICKLE_LIMIT)
    job.step_done = 0
    _log(job, f"stems: {len(targets)} missing; trickling {job.step_total}")
    if job.step_total:
        if job.scope == "track":
            stable_id, _path = targets[0]
            _run_cli(
                job,
                [sys.executable, "-m", "apps.stems", "one", "--stable-id", stable_id, "--live"],
            )
            job.step_done = 1
            return
        _run_cli(
            job,
            [sys.executable, "-m", "apps.stems", "trickle", "--live",
             "--limit", str(STEMS_TRICKLE_LIMIT)],
        )
        job.step_done = job.step_total


def _step_vocals(job: _RefreshJob, targets: list[tuple[str, str]]) -> None:
    job.step_total = len(targets)
    job.step_done = 0
    _log(job, f"vocals: {len(targets)} missing cache; from-stems backfill")
    argv = [sys.executable, "-m", "apps.vocals", "from-stems", "--live"]
    if job.scope == "track":
        stable_id, _path = targets[0]
        argv += ["--stable-id", stable_id]
    _run_cli(job, argv)
    job.step_done = job.step_total


def _fail_unhandled_step_runner(
    job: _RefreshJob, _targets: list[tuple[str, str]]
) -> NoReturn:  # pragma: no cover - registry and worker must agree
    raise RuntimeError(f"unhandled step {job.current_step}")


# Exhaustiveness guard lives in the dispatch: every STEPS id needs a runner.
_STEP_RUNNERS = {
    "analysis": _step_analysis,
    "stems": _step_stems,
    "vocals": _step_vocals,
}


def unmapped_backlog(*, limit: int | None = None) -> backlog.Backlog:
    """The analyze-on-import queue. One accessor for its three readers (the
    drain, GET /analysis-queue, the auto-drain) so they cannot disagree about
    which state DB it lives in."""
    conn = open_ro()
    try:
        return backlog.scan(conn, limit=limit)
    finally:
        conn.close()


def _log_id_keyed_skips(job: _RefreshJob, steps: list[str], scope: str) -> None:
    """Say which steps this scope cannot run, and why. Never silently."""
    for skipped in steps:
        _log(job, f"{skipped}: skipped for {scope} scope - needs the "
                  "Rekordbox import first (id-keyed)")


def _batch_targets(
    job: _RefreshJob, _roots: tuple[Path, ...]
) -> dict[str, list[tuple[str, str]]]:
    """Freshly staged files with no state.db rows yet, so no ids either."""
    assert job.batch_dir is not None
    staged = sorted(
        str(p) for p in job.batch_dir.rglob("*")
        if p.suffix.lower() in AUDIO_EXTENSIONS
    )
    _log(job, f"batch scope: {len(staged)} staged files in {job.batch_dir}")
    _log_id_keyed_skips(job, [s for s in job.steps if s != "analysis"], "batch")
    job.steps = [s for s in job.steps if s == "analysis"]
    return {"analysis": [("", f) for f in staged], "stems": [], "vocals": []}


def _unmapped_targets(
    job: _RefreshJob, _roots: tuple[Path, ...]
) -> dict[str, list[tuple[str, str]]]:
    """Locally imported tracks: a tracks row, no live rekordbox twin."""
    queue = unmapped_backlog()
    job.queue_signature = queue.signature
    _log(job, f"unmapped scope: {queue.pending_total} of {queue.unmapped} "
              f"rekordbox-unmapped tracks need analysis ({queue.analyzed} already "
              f"analyzed, {queue.unreachable} unreachable)")
    _log_id_keyed_skips(job, job.skipped_steps, UNMAPPED_SCOPE)
    return {
        "analysis": [(i.stable_id, i.file_path) for i in queue.pending],
        "stems": [],
        "vocals": [],
    }


def _library_targets(
    job: _RefreshJob, roots: tuple[Path, ...]
) -> dict[str, list[tuple[str, str]]]:
    on_disk, unreachable = tracks_on_disk(open_ro)
    _log(job, f"coverage: {len(on_disk)} tracks on disk, {unreachable} unreachable")
    # Refresh targets stay unified with coverage's "missing" semantics: a
    # corrupt entry still needs its step re-run, exactly like an absent or
    # stale one, so it is a target here too. Only the coverage ROUTE splits
    # corrupt out as an additional, distinguishing signal for the UI.
    missing, _corrupt = missing_by_step(
        on_disk, open_ro, roots, VOCAL_CACHE_DIR, LYRICS_CACHE_DIR
    )
    return missing


def validate_track_order_target(stable_id: str) -> None:
    """Fail before queueing when a requested track is absent or ambiguous."""
    targets, _unreachable = tracks_on_disk(open_ro)
    select_track_target(stable_id, targets)


def _track_targets(
    job: _RefreshJob, _roots: tuple[Path, ...]
) -> dict[str, list[tuple[str, str]]]:
    """One explicit track order, selected before the shared worker starts."""
    if len(job.analysis_orders) != 1:
        raise RuntimeError("track scope requires exactly one analysis order")
    stable_id, kind = next(iter(job.analysis_orders.items()))
    step = "stems" if kind == "stems" else "vocals" if kind == "vocals" else "analysis"
    targets, _unreachable = tracks_on_disk(open_ro)
    selected = [select_track_target(stable_id, targets)]
    _log(job, f"track scope: ordering {kind} for {stable_id} through {step}")
    return {"analysis": selected if step == "analysis" else [],
            "stems": selected if step == "stems" else [],
            "vocals": selected if step == "vocals" else []}


# Exhaustiveness guard in the dispatch, same as _STEP_RUNNERS below.
_SCOPE_TARGETS = {
    "batch": _batch_targets,
    UNMAPPED_SCOPE: _unmapped_targets,
    "library": _library_targets,
    "track": _track_targets,
}


def _targets_for(
    job: _RefreshJob, roots: tuple[Path, ...]
) -> dict[str, list[tuple[str, str]]]:
    """Per-scope work list. An unknown scope is a programming error, not a run."""
    build = _SCOPE_TARGETS.get(job.scope)
    if build is None:  # pragma: no cover - start_refresh validates the scope
        raise RuntimeError(f"unhandled refresh scope {job.scope!r}")
    return build(job, roots)


def _refresh_worker(job: _RefreshJob, roots: tuple[Path, ...]) -> None:
    try:
        job.phase = "running"
        missing = _targets_for(job, roots)

        for step in job.steps:
            job.current_step = step
            _STEP_RUNNERS.get(step, _fail_unhandled_step_runner)(job, missing[step])
            job.steps_completed.append(step)

        job.phase = "done"
        _log(job, "refresh complete")
    except Exception as exc:  # noqa: BLE001 - job boundary, surfaced via status
        job.phase = "error"
        job.error = str(exc)
        _log(job, f"ERROR: {exc}")
    finally:
        job.current_step = None
        job.finished_at = time.time()
        publish("library.changed", {"kind": "tracks", "ids": []})


def _start_refresh_job(
    body: RefreshIn | None,
    roots: tuple[Path, ...],
    guard: Callable[[], None] | None = None,
) -> _RefreshJob:
    """Claim the one slot and hand back THE job created, not the slot.

    A caller that needs its own job has to be given it here. Rereading
    ``_JOBS.current`` afterwards is a race with a horizon of one statement: a
    short drain can finish and a second request claim the slot in between,
    leaving the caller holding somebody else's job.

    ``guard`` runs UNDER the lock, so a caller whose decision to start depends
    on the registry decides and claims in one step; it refuses by raising.
    """
    scope, batch_dir = resolve_scope(body, INGEST_INBOX)
    with _job_lock:
        if _JOBS.current is not None and _JOBS.current.phase in ACTIVE_PHASES:
            raise HTTPException(409, "a refresh job is already running")
        if guard is not None:
            guard()
        enabled = _load_enabled()
        steps = [s["id"] for s in STEPS if enabled[s["id"]]]
        if not steps:
            raise HTTPException(422, "no steps enabled in ingest config")
        skipped: list[str] = []
        if scope == UNMAPPED_SCOPE:
            steps, skipped = unmapped_steps(steps)
        if scope == "track":
            assert body is not None and body.analysis_kind is not None
            track_step = (
                "stems" if body.analysis_kind == "stems"
                else "vocals" if body.analysis_kind == "vocals"
                else "analysis"
            )
            if track_step not in steps:
                raise HTTPException(
                    422, f"track {body.analysis_kind} requires the {track_step} step enabled"
                )
            skipped = [step for step in steps if step != track_step]
            steps = [track_step]
        is_track_order = (
            scope == "track"
            and body is not None
            and body.stable_id is not None
            and body.analysis_kind is not None
        )
        orders = {body.stable_id: body.analysis_kind} if is_track_order and body is not None else {}
        job = _RefreshJob(started_at=time.time(), steps=steps, scope=scope,
                          batch_dir=batch_dir, skipped_steps=skipped, analysis_orders=orders)
        _JOBS.current = job
        if scope == UNMAPPED_SCOPE:
            _JOBS.last_unmapped = job
        threading.Thread(
            target=_refresh_worker, args=(job, roots), daemon=True
        ).start()
    return job


@router.post("/refresh", response_model=RefreshStatusOut, status_code=202)
def start_refresh(request: Request, body: RefreshIn | None = None) -> RefreshStatusOut:
    return _status_of(_start_refresh_job(body, _stem_roots(request.app)))


@router.get("/refresh/status", response_model=RefreshStatusOut)
def refresh_status() -> RefreshStatusOut:
    return _status_of(_JOBS.current)


def _status_of(job: _RefreshJob | None) -> RefreshStatusOut:
    """Render one job as the wire status - the slot, or a job by name.

    Split so the POST reports the job it started, not whatever holds the slot
    when it renders: the same one-statement race closed above.
    """
    if job is None:
        return RefreshStatusOut(
            running=False, phase="idle", steps=[], current_step=None,
            step_done=0, step_total=0, steps_completed=[], started_at=None,
            finished_at=None, error=None, log_tail=[], recently_done_ids=[],
        )
    return RefreshStatusOut(
        running=job.phase in ACTIVE_PHASES, phase=job.phase, steps=job.steps,
        current_step=job.current_step, step_done=job.step_done,
        step_total=job.step_total, steps_completed=job.steps_completed,
        started_at=job.started_at, finished_at=job.finished_at, error=job.error,
        log_tail=list(job.log)[-LOG_TAIL_LINES:],
        recently_done_ids=list(job.recently_done_ids),
    )
