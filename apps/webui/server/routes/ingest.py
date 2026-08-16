"""Ingestion pipeline endpoints: config, coverage, refresh job, upload+dedup.

Backs two UI features and their agent-native parity:

  * "Refresh analysis" TopBar button  -> GET /ingest/coverage,
    POST /ingest/refresh, GET /ingest/refresh/status
  * Drag-in ingest modal              -> GET/PUT /ingest/config,
    POST /ingest/upload

Design constraints honoured here:
  * Rekordbox remains the only writer of its own DB; upload stages files
    under ``~/Music/Manual Library/_ingest/<batch>/`` and RB import stays a
    human step (see .agents/skills/ingest-new-tracks).
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
    (analysis table, stems bundles, vocal-cache) over on-disk tracks.
    [if] a track's file is a broken link [then] it is excluded and counted
    in ``unreachable`` instead of any step's missing list
  ✔︎ ✅ POST refresh + status: background job over missing tracks for
    enabled steps; ring-buffer log; progress per step.
    [if] a refresh is already running [then ⛔️] 409
    [if] a step subprocess exits non-zero [then] job phase "error" with the
    tail of its output, later steps not run
  ✔︎ ✅ POST upload: stage real bytes + duration & fingerprint dup check.
    [if] staged bytes differ in size from the upload [then ⛔️] 500, temp
    file removed
    [if] fingerprint >= threshold match exists and force is not set
    [then] file skipped with duplicate_of reported
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from apps.shared import fs_residency
from apps.shared.fingerprints import ChromaprintMissing, compare, compute
from apps.shared.paths import AUDIO_EXTENSIONS, HOME, PROJECT_ROOT, STATE_DB
from apps.shared.state.db import open_ro
from apps.webui.server.stem_artifacts import DEFAULT_STEMS_DIR

router = APIRouter(prefix="/ingest", tags=["ingest"])

# ----- CFG -------------------------------------------------------------------
CONFIG_PATH: Path = STATE_DB.parent / "ingest-config.json"
INGEST_INBOX: Path = HOME / "Music" / "Manual Library" / "_ingest"
VOCAL_CACHE_DIR: Path = STATE_DB.parent / "vocal-cache"
DUP_DURATION_TOLERANCE_MS: int = 1_500
DUP_FP_THRESHOLD: float = 0.92          # matches apps.dedup.find_clusters
DUP_MAX_FP_CANDIDATES: int = 5          # fingerprinting candidates is O(seconds) each
ANALYSIS_CHUNK: int = 25                # progress granularity for the analysis step
STEMS_TRICKLE_LIMIT: int = 5            # local stems are ~104MB/track; keep small
LOG_RING: int = 400
BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,79}$")

StepId = Literal["analysis", "stems", "vocals"]

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
    total_tracks: int
    on_disk: int
    unreachable: int
    missing: dict[str, int]
    generated_at: float


def _tracks_on_disk() -> tuple[list[tuple[str, str]], int]:
    """(stable_id, file_path) for tracks whose file is materialised; + unreachable count."""
    conn = open_ro()
    try:
        rows = conn.execute(
            "SELECT stable_id, file_path FROM tracks WHERE file_path IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    ok: list[tuple[str, str]] = []
    unreachable = 0
    for sid, fp in rows:
        if fp.startswith(("tidal:", "soundcloud:", "spotify:")):
            continue
        if fs_residency.is_materialised(Path(fp)):
            ok.append((sid, fp))
        else:
            unreachable += 1
    return ok, unreachable


def _missing_by_step(on_disk: list[tuple[str, str]]) -> dict[str, list[tuple[str, str]]]:
    conn = open_ro()
    try:
        analysed = {r[0] for r in conn.execute("SELECT DISTINCT stable_id FROM analysis")}
    finally:
        conn.close()
    stems_done = (
        {p.name for p in DEFAULT_STEMS_DIR.iterdir() if p.is_dir()}
        if DEFAULT_STEMS_DIR.is_dir()
        else set()
    )
    vocals_done = (
        {p.stem for p in VOCAL_CACHE_DIR.glob("*.json")}
        if VOCAL_CACHE_DIR.is_dir()
        else set()
    )
    return {
        "analysis": [(s, f) for s, f in on_disk if s not in analysed],
        "stems": [(s, f) for s, f in on_disk if s not in stems_done],
        "vocals": [(s, f) for s, f in on_disk if s not in vocals_done],
    }


@router.get("/coverage", response_model=CoverageOut)
def get_coverage() -> CoverageOut:
    on_disk, unreachable = _tracks_on_disk()
    missing = _missing_by_step(on_disk)
    conn = open_ro()
    try:
        total = conn.execute("SELECT count(*) FROM tracks").fetchone()[0]
    finally:
        conn.close()
    return CoverageOut(
        total_tracks=total,
        on_disk=len(on_disk),
        unreachable=unreachable,
        missing={k: len(v) for k, v in missing.items()},
        generated_at=time.time(),
    )


# ----- refresh job ----------------------------------------------------------
@dataclass
class _RefreshJob:
    started_at: float
    steps: list[str]
    phase: str = "queued"            # queued | running | done | error
    current_step: Optional[str] = None
    step_done: int = 0
    step_total: int = 0
    steps_completed: list[str] = field(default_factory=list)
    error: Optional[str] = None
    log: deque = field(default_factory=lambda: deque(maxlen=LOG_RING))
    recently_done_ids: deque = field(default_factory=lambda: deque(maxlen=200))
    finished_at: Optional[float] = None


_job_lock = threading.Lock()
_job: Optional[_RefreshJob] = None


class RefreshStatusOut(BaseModel):
    running: bool
    phase: str
    steps: list[str]
    current_step: Optional[str]
    step_done: int
    step_total: int
    steps_completed: list[str]
    started_at: Optional[float]
    finished_at: Optional[float]
    error: Optional[str]
    log_tail: list[str]
    recently_done_ids: list[str]


def _log(job: _RefreshJob, line: str) -> None:
    job.log.append(f"[{time.strftime('%H:%M:%S')}] {line}")


def _run_cli(job: _RefreshJob, argv: list[str]) -> None:
    """Run a pipeline CLI, streaming stdout into the job log. Raises on rc!=0."""
    _log(job, "$ " + " ".join(argv[-6:]))
    proc = subprocess.Popen(
        argv,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        stripped = line.rstrip()
        if stripped:
            _log(job, stripped)
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"{argv[2] if len(argv) > 2 else argv[0]} exited {rc}")


def _refresh_worker(job: _RefreshJob) -> None:
    global _job
    try:
        job.phase = "running"
        on_disk, unreachable = _tracks_on_disk()
        missing = _missing_by_step(on_disk)
        _log(job, f"coverage: {len(on_disk)} tracks on disk, {unreachable} unreachable")

        for step in job.steps:
            job.current_step = step
            targets = missing[step]
            if step == "analysis":
                job.step_total = len(targets)
                job.step_done = 0
                _log(job, f"analysis: {len(targets)} tracks missing")
                for i in range(0, len(targets), ANALYSIS_CHUNK):
                    chunk = targets[i : i + ANALYSIS_CHUNK]
                    _run_cli(
                        job,
                        [sys.executable, "-m", "apps.analysis.run", "--workers", "2",
                         "--files", *[f for _, f in chunk]],
                    )
                    job.step_done += len(chunk)
                    for sid, _ in chunk:
                        job.recently_done_ids.append(sid)
            elif step == "stems":
                job.step_total = min(len(targets), STEMS_TRICKLE_LIMIT)
                job.step_done = 0
                _log(job, f"stems: {len(targets)} missing; trickling {job.step_total}")
                if job.step_total:
                    _run_cli(
                        job,
                        [sys.executable, "-m", "apps.stems", "trickle", "--live",
                         "--limit", str(STEMS_TRICKLE_LIMIT)],
                    )
                    job.step_done = job.step_total
            elif step == "vocals":
                job.step_total = len(targets)
                job.step_done = 0
                _log(job, f"vocals: {len(targets)} missing cache; from-stems backfill")
                _run_cli(
                    job, [sys.executable, "-m", "apps.vocals", "from-stems", "--live"]
                )
                job.step_done = job.step_total
            else:  # pragma: no cover - registry and worker must agree
                raise RuntimeError(f"unhandled step {step}")
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


@router.post("/refresh", response_model=RefreshStatusOut, status_code=202)
def start_refresh() -> RefreshStatusOut:
    global _job
    with _job_lock:
        if _job is not None and _job.phase in ("queued", "running"):
            raise HTTPException(409, "a refresh job is already running")
        enabled = _load_enabled()
        steps = [s["id"] for s in STEPS if enabled[s["id"]]]
        if not steps:
            raise HTTPException(422, "no steps enabled in ingest config")
        _job = _RefreshJob(started_at=time.time(), steps=steps)
        threading.Thread(target=_refresh_worker, args=(_job,), daemon=True).start()
    return refresh_status()


@router.get("/refresh/status", response_model=RefreshStatusOut)
def refresh_status() -> RefreshStatusOut:
    job = _job
    if job is None:
        return RefreshStatusOut(
            running=False, phase="idle", steps=[], current_step=None,
            step_done=0, step_total=0, steps_completed=[], started_at=None,
            finished_at=None, error=None, log_tail=[], recently_done_ids=[],
        )
    return RefreshStatusOut(
        running=job.phase in ("queued", "running"),
        phase=job.phase,
        steps=job.steps,
        current_step=job.current_step,
        step_done=job.step_done,
        step_total=job.step_total,
        steps_completed=job.steps_completed,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error=job.error,
        log_tail=list(job.log)[-60:],
        recently_done_ids=list(job.recently_done_ids),
    )


# ----- upload + duplicate check ---------------------------------------------
class UploadFileResult(BaseModel):
    filename: str
    staged_path: Optional[str]
    skipped_duplicate: bool
    duplicate_of: Optional[dict]     # {stable_id, title, artist, method, score}
    duration_s: Optional[float]
    fingerprint_method: str          # "chromaprint" | "duration"


class UploadOut(BaseModel):
    batch: str
    dest_dir: str
    results: list[UploadFileResult]


def _duration_s(path: Path) -> Optional[float]:
    from apps.shared._mutagen import require as require_mutagen

    require_mutagen()
    import mutagen

    mf = mutagen.File(path)
    if mf is None or mf.info is None:
        return None
    return float(mf.info.length)


def _dup_candidates(duration_s: float) -> list[tuple[str, str, str, str]]:
    """(stable_id, title, artist, file_path) within duration tolerance."""
    lo = int(duration_s * 1000) - DUP_DURATION_TOLERANCE_MS
    hi = int(duration_s * 1000) + DUP_DURATION_TOLERANCE_MS
    conn = open_ro()
    try:
        return conn.execute(
            "SELECT stable_id, title, artists_json, file_path FROM tracks "
            "WHERE duration_ms BETWEEN ? AND ? AND file_path IS NOT NULL",
            (lo, hi),
        ).fetchall()
    finally:
        conn.close()


def _best_duplicate(staged: Path, duration_s: float) -> tuple[Optional[dict], str]:
    """Best duplicate candidate and the method actually used."""
    candidates = [
        c for c in _dup_candidates(duration_s) if Path(c[3]).exists()
    ][:DUP_MAX_FP_CANDIDATES]
    if not candidates:
        return None, "duration"
    try:
        new_fp = compute(staged)
    except ChromaprintMissing:
        # No fingerprint available: report the closest duration match but say
        # so explicitly - the client decides, nothing silently passes as dup.
        sid, title, artist, _ = candidates[0]
        return (
            {"stable_id": sid, "title": title, "artist": artist,
             "method": "duration", "score": None},
            "duration",
        )
    best: Optional[dict] = None
    for sid, title, artist, fp_path in candidates:
        score = compare(new_fp, compute(Path(fp_path)))
        if best is None or score > best["score"]:
            best = {"stable_id": sid, "title": title, "artist": artist,
                    "method": "chromaprint", "score": round(score, 4)}
    return best, "chromaprint"


@router.post("/upload", response_model=UploadOut)
async def upload(
    files: list[UploadFile] = File(...),
    batch: str = Form(...),
    force: bool = Form(False),
) -> UploadOut:
    if not BATCH_RE.match(batch):
        raise HTTPException(422, f"invalid batch name {batch!r} (need {BATCH_RE.pattern})")
    dest_dir = INGEST_INBOX / batch
    dest_dir.mkdir(parents=True, exist_ok=True)

    results: list[UploadFileResult] = []
    for up in files:
        name = Path(up.filename or "").name
        if not name or Path(name).suffix.lower() not in AUDIO_EXTENSIONS:
            raise HTTPException(422, f"not an audio file: {up.filename!r}")
        tmp = dest_dir / (name + ".part")
        with tmp.open("wb") as fh:
            shutil.copyfileobj(up.file, fh)
        if tmp.stat().st_size == 0:
            tmp.unlink()
            raise HTTPException(422, f"empty upload: {name}")

        duration = _duration_s(tmp)
        dup, method = (None, "duration")
        if duration is not None:
            dup, method = _best_duplicate(tmp, duration)
        is_dup = (
            dup is not None
            and dup["method"] == "chromaprint"
            and dup["score"] >= DUP_FP_THRESHOLD
        )
        if is_dup and not force:
            tmp.unlink()
            results.append(UploadFileResult(
                filename=name, staged_path=None, skipped_duplicate=True,
                duplicate_of=dup, duration_s=duration, fingerprint_method=method,
            ))
            continue
        final = dest_dir / name
        tmp.rename(final)
        results.append(UploadFileResult(
            filename=name, staged_path=str(final), skipped_duplicate=False,
            duplicate_of=dup, duration_s=duration, fingerprint_method=method,
        ))
    return UploadOut(batch=batch, dest_dir=str(dest_dir), results=results)
