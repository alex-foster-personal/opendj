"""Deterministic lyric batch driver - operational-plan section 12, R1/R2.

ONE driver sequencing every pipeline stage the batch-1/batch-2 rounds ran by
hand (specs/karaoke-lyrics-alignment.md), with ZERO LLM steps in the happy
path: corpus staging + metadata join, Modal stems (skipping tracks already
holding a canonical bundle), register_pair immediately per pair (the batch-1
sub-rung-A lesson: stems that miss registration are stems lost to the
product), stem coverage, candidate fetch (LRCLIB always; the Musixmatch tiers
only when MUSIXMATCH_API_KEY is present - and it SAYS so when not), lingua
language pin, language-pinned ASR witness, version screen, post-screen
language re-pin, make-jobs, MMS align, manifest (witness verdicts), state.db
ingest, and a ledger-append STUB (prints the exact kpi command; a human/agent
stamps snapshots, per the KPI-ledger house rule).

Failure contract (R1):
- a stage failing for ONE track excludes that track BY NAME and the run
  continues;
- a whole stage being impossible (Modal unreachable, no corpus dir, ...)
  raises StageBlocked -> the CLI exits non-zero naming the blocker;
- a stem pair left unregistered after the register stage ABORTS the run.

Overlap (R2): the stems+register+coverage lane and the candidates+language
lane have independent inputs and run concurrently via a two-thread executor;
everything after the join is strictly sequential. Nothing fancier.

The heavy externals are invoked as SUBPROCESS COMMAND LISTS built here and
executed by an injectable ``runner`` callable (default: subprocess.run with
check). Tests inject a recorder that asserts the exact commands and
fabricates stage outputs, while register_pair and ingest-state run REAL.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from apps.cloud import policy, stem_index
from apps.cloud.asset_store import require_credentials
from apps.cloud.config import CloudConfig, MissingEnvError
from apps.cloud.eviction import HydrationError
from apps.cloud.hydration_core import resolve_policy
from apps.cloud.stem_hydration import hydrate_one
from apps.cloud.stem_source import resolve_stem_hydration_source
from apps.lyrics import store
from apps.lyrics.artifacts import asset_clients_for_mode
from apps.lyrics.ingest_state import ingest_state
from apps.lyrics.register_stems import (
    RegisterPairStorage,
    _corpus_pairs,
    _meta_model,
    register_pair,
)
from apps.shared.paths import PROJECT_ROOT, STATE_DB, STATE_DIR
from apps.shared.state import db as state_db_mod
from apps.shared.state import locations as state_locations
from apps.shared.state import sync_stamp
from apps.stems.artifacts import ROFORMER_STEMS_DIR

REPO_ROOT: Path = PROJECT_ROOT
LYRICS_EVAL_DIR: Path = STATE_DIR / "lyrics-eval"
BENCH_DIR: Path = REPO_ROOT / "scripts" / "bench"
UV_PY: tuple[str, ...] = ("uv", "run", "--no-sync", "python")
UV_MODAL_PY: tuple[str, ...] = ("uv", "run", "--with", "modal", "python")
UV_SCRIPT: tuple[str, ...] = ("uv", "run", "--script")
SPIKE: str = "scripts/lyrics_oltf_spike.py"
METADATA_SCRIPT: str = "scripts/lyrics_crate_metadata.py"
ROFORMER_SPIKE: str = "scripts/modal_roformer_spike.py"
COVERAGE_SCRIPT: str = "scripts/lyrics_stem_coverage.py"
LANGUAGE_SCRIPT: str = "scripts/lyrics_oltf_language.py"
ASR_SPIKE: str = "scripts/modal_asr_spike.py"
ALIGN_SPIKE: str = "scripts/modal_align_spike.py"
MANIFEST_SCRIPT: str = "scripts/bench/lyrics_owncrate_manifest.py"
KPI_APPEND_SCRIPT: str = "scripts/bench/lyrics_kpi_append.py"
ASR_DIR_NAME: str = "asr-pin"  # language-pinned transcripts (oltf round-2 lesson)
PRED_DIR_NAME: str = "pred"
MUSIXMATCH_ENV_KEY: str = "MUSIXMATCH_API_KEY"
# The batch-2 hallucination fix: these ISO3 codes must be pinned in
# modal_asr_spike.ISO3_TO_WHISPER. Checked textually (importing the spike
# would drag modal into the repo venv, against the heavy-deps house rule).
REQUIRED_ISO3_PINS: tuple[str, ...] = ("ukr", "est", "tgl", "swa")

Runner = Callable[[list[str]], None]
Progress = Callable[[str], None]


class CommandFailed(RuntimeError):
    """A stage subprocess exited non-zero (raised by the runner)."""


class StageBlocked(RuntimeError):
    """A whole stage is impossible - the run exits non-zero naming it (R1)."""


def run_subprocess(cmd: list[str]) -> None:
    """Default runner: stream output live, fail loudly on non-zero exit."""
    proc = subprocess.run(cmd, cwd=REPO_ROOT, check=False)
    if proc.returncode != 0:
        raise CommandFailed(f"command failed ({proc.returncode}): {' '.join(cmd)}")


def _subprocess_runner_for(paths: BatchPaths) -> Runner:
    """Bind ``BatchPaths`` so subprocess stages honor ``--state-dir`` / ``MDT_DATA_DIR``."""
    default_paths = BatchPaths()

    def runner(cmd: list[str]) -> None:
        env = os.environ.copy()
        if paths != default_paths:
            env["MDT_DATA_DIR"] = str(paths.data_dir)
        proc = subprocess.run(cmd, cwd=REPO_ROOT, check=False, env=env)
        if proc.returncode != 0:
            raise CommandFailed(f"command failed ({proc.returncode}): {' '.join(cmd)}")

    return runner


#-----------------------------------------------------------------------------
# plan + report
#-----------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchPaths:
    eval_dir: Path = LYRICS_EVAL_DIR
    state_db: Path = STATE_DB
    stems_root: Path = ROFORMER_STEMS_DIR
    bench_dir: Path = BENCH_DIR

    @property
    def data_dir(self) -> Path:
        return self.state_db.parent.parent


def batch_paths_for(state_dir: Path) -> BatchPaths:
    """Resolve batch driver paths from a state dir holding state.db."""
    return BatchPaths(
        eval_dir=state_dir / "lyrics-eval",
        state_db=state_dir / "state.db",
        stems_root=state_dir / "stems-roformer-spike",
        bench_dir=REPO_ROOT / "scripts" / "bench",
    )


@dataclass
class TrackPlan:
    stable_id: str
    track_id: str
    db_file_path: str
    audio_path: Path | None  # resolved on-disk audio; None = missing here
    registered: bool  # canonical bundle already exists (skip separation)


@dataclass
class BatchReport:
    corpus: str
    live: bool
    tracks: list[TrackPlan] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)  # track_id -> reason
    stages: list[str] = field(default_factory=list)
    aligned: list[str] = field(default_factory=list)  # track_ids through align
    ingested: int = 0

    def exclude(self, track_id: str, reason: str) -> None:
        self.excluded[track_id] = reason

    def active(self) -> list[TrackPlan]:
        return [t for t in self.tracks if t.track_id not in self.excluded]

    def summary(self) -> str:
        lines = [
            f"batch {self.corpus}: {len(self.tracks)} staged, "
            f"aligned {len(self.aligned)}, ingested {self.ingested}, "
            f"excluded {len(self.excluded)} "
            f"(denominator: {len(self.tracks)} requested tracks)"
        ]
        for track_id, reason in sorted(self.excluded.items()):
            lines.append(f"  excluded {track_id}: {reason}")
        return "\n".join(lines)


#-----------------------------------------------------------------------------
# stage commands (single source of truth for dry-run print AND execution)
#-----------------------------------------------------------------------------


class Cmds:
    """Command-list builders for every subprocess stage."""

    def __init__(self, corpus: str, corpus_dir: Path, bench_dir: Path,
                 state_dir: Path) -> None:
        self.corpus = corpus
        self.dir = corpus_dir
        self.bench_dir = bench_dir
        self.state_dir = state_dir

    def metadata(self) -> list[str]:
        return [*UV_PY, METADATA_SCRIPT, "--corpus", self.corpus,
                "--state-dir", str(self.state_dir)]

    def stems(self, inputs: list[str]) -> list[str]:
        return [*UV_MODAL_PY, ROFORMER_SPIKE, "separate",
                "--input", *inputs, "--out-dir", str(self.dir / "stems")]

    def coverage(self) -> list[str]:
        return [*UV_SCRIPT, COVERAGE_SCRIPT, "--stems", str(self.dir / "stems"),
                "--out", str(self.dir / "vocal-presence.json")]

    def spike(self, *args: str) -> list[str]:
        return [*UV_PY, SPIKE, "--dir", self.corpus,
                "--state-dir", str(self.state_dir), *args]

    def merge_musixmatch(self) -> list[str]:
        return [*UV_PY, "-m", "apps.lyrics.sources", "merge-musixmatch"]

    def language(self) -> list[str]:
        return [*UV_SCRIPT, LANGUAGE_SCRIPT, "--dir", self.corpus,
                "--state-dir", str(self.state_dir)]

    def asr(self) -> list[str]:
        return [*UV_MODAL_PY, ASR_SPIKE, "transcribe-files",
                "--jobs", str(self.dir / "asr-jobs.json"),
                "--out-dir", str(self.dir / ASR_DIR_NAME)]

    def align(self) -> list[str]:
        return [*UV_MODAL_PY, ALIGN_SPIKE, "align-files",
                "--jobs", str(self.dir / "jobs.json"),
                "--pred-dir", str(self.dir / PRED_DIR_NAME)]

    def manifest(self, jobs_path: Path) -> list[str]:
        return [*UV_PY, MANIFEST_SCRIPT,
                "--jobs", str(jobs_path),
                "--pred", str(self.dir / PRED_DIR_NAME),
                "--asr", str(self.dir / ASR_DIR_NAME),
                "--audio", str(self.dir / "audio"),
                "--stems", str(self.dir / "stems"),
                "--out", str(self.manifest_out()),
                "--mount", f"/{self.corpus}"]

    def manifest_out(self) -> Path:
        return self.bench_dir / f"lyrics_{self.corpus}_manifest.json"


#-----------------------------------------------------------------------------
# resolution
#-----------------------------------------------------------------------------


def _resolve_tracks(
    corpus: str, stable_ids: list[str], paths: BatchPaths, report: BatchReport,
) -> None:
    if not stable_ids:
        raise StageBlocked("resolve: zero stable_ids requested")
    if len(set(stable_ids)) != len(stable_ids):
        raise StageBlocked("resolve: duplicate stable_ids in the request")
    if not paths.state_db.is_file():
        raise StageBlocked(f"resolve: state.db missing at {paths.state_db}")
    conn = sqlite3.connect(f"file:{paths.state_db}?mode=ro", uri=True)
    try:
        marks = ",".join("?" for _ in stable_ids)
        rows = dict(conn.execute(
            f"SELECT stable_id, file_path FROM tracks "
            f"WHERE stable_id IN ({marks}) AND deleted_at IS NULL",
            stable_ids,
        ).fetchall())
        audio_paths = state_locations.bulk_local_audio_paths(conn, stable_ids)
    finally:
        conn.close()
    unknown = [sid for sid in stable_ids if sid not in rows]
    if unknown:
        raise StageBlocked(f"resolve: stable_ids not in state.db tracks: {unknown}")
    for i, sid in enumerate(stable_ids):
        track_id = f"{corpus}{i:03d}"
        db_file_path = rows[sid] or ""
        audio = audio_paths.get(sid)
        registered = (paths.stems_root / sid / "manifest.json").is_file()
        plan = TrackPlan(stable_id=sid, track_id=track_id,
                         db_file_path=db_file_path, audio_path=audio,
                         registered=registered)
        report.tracks.append(plan)
        if audio is None and not registered:
            report.exclude(track_id, f"resolve: audio missing on this Mac "
                                     f"({db_file_path or 'no file_path'})")


def _preflight_r2_credentials() -> None:
    """Refuse cloud live runs before Modal when R2 credentials are absent."""
    if policy.CFG.mode != "cloud":
        return
    try:
        cfg = CloudConfig.from_env()
        require_credentials(cfg)
    except MissingEnvError as exc:
        raise StageBlocked(str(exc)) from exc


def _assert_iso3_pins(repo_root: Path) -> None:
    spike = repo_root / ASR_SPIKE
    if not spike.is_file():
        raise StageBlocked(f"preflight: {ASR_SPIKE} missing under {repo_root}")
    text = spike.read_text(encoding="utf-8")
    missing = [c for c in REQUIRED_ISO3_PINS if f'"{c}"' not in text]
    if missing:
        raise StageBlocked(
            f"preflight: ISO3_TO_WHISPER lacks the batch-2 pins {missing} "
            f"(the oltf/batch-2 hallucination class) - fix {ASR_SPIKE} first")


#-----------------------------------------------------------------------------
# stages
#-----------------------------------------------------------------------------


def _stage_corpus(cmds: Cmds, report: BatchReport, runner: Runner,
                  progress: Progress) -> None:
    """tracks.json + audio symlinks, then the real state.db metadata join."""
    stageable = [t for t in report.active() if t.audio_path is not None
                 or t.registered]
    corpus_dir = cmds.dir
    corpus_dir.mkdir(parents=True, exist_ok=True)
    (corpus_dir / "audio").mkdir(exist_ok=True)
    (corpus_dir / "stems").mkdir(exist_ok=True)
    tracks_json = corpus_dir / "tracks.json"
    rows = []
    for t in stageable:
        if t.audio_path is None:
            # Registered bundle but no local mix: alignment needs only the
            # stem, but the spike's staging contract needs a real source_path.
            report.exclude(t.track_id, "corpus: registered bundle but no local "
                                       "audio to stage")
            continue
        rows.append({"track_id": t.track_id, "basename": t.audio_path.name,
                     "source_path": str(t.audio_path)})
        link = corpus_dir / "audio" / f"{t.track_id}{t.audio_path.suffix.lower()}"
        if not link.is_symlink() and not link.exists():
            link.symlink_to(t.audio_path)
    if not rows:
        raise StageBlocked("corpus: no requested track has resolvable audio")
    if tracks_json.is_file():
        staged = {r["track_id"]: r["source_path"]
                  for r in json.loads(tracks_json.read_text(encoding="utf-8"))}
        wanted = {r["track_id"]: r["source_path"] for r in rows}
        if staged != wanted:
            raise StageBlocked(
                f"corpus: {tracks_json} already staged with different rows - "
                f"pick a fresh corpus name or clear the dir")
    else:
        tracks_json.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    progress(f"corpus: staged {len(rows)} rows -> {tracks_json}")
    runner(cmds.metadata())
    plan_by_id = {t.track_id: t for t in report.tracks}
    for row in json.loads(tracks_json.read_text(encoding="utf-8")):
        plan = plan_by_id[row["track_id"]]
        if row.get("excluded_reason"):
            report.exclude(row["track_id"], f"metadata: {row['excluded_reason']}")
        elif row["stable_id"] != plan.stable_id:
            report.exclude(row["track_id"],
                           f"metadata: joined stable_id {row['stable_id']} != "
                           f"requested {plan.stable_id}")
    report.stages.append("corpus")


def _lane_stems(cmds: Cmds, report: BatchReport, runner: Runner,
                progress: Progress, paths: BatchPaths) -> None:
    """Stems (skip already-registered) -> register_pair per pair -> coverage."""
    stems_root = paths.stems_root
    stems_dir = cmds.dir / "stems"
    data_dir = paths.data_dir
    index = stem_index.load_cached_index(data_dir)
    source = resolve_stem_hydration_source(data_dir)
    for t in report.active():
        if t.registered or t.stable_id not in index:
            continue
        if source is None:
            progress(f"stems: {t.stable_id} in fleet index but hydration "
                     "transport unarmed")
            continue
        outcome = hydrate_one(
            t.stable_id,
            data_dir=data_dir,
            source=source,
            index=index,
            stems_dir=stems_root,
        )
        if outcome.status in ("already_local", "hydrated"):
            t.registered = True
            progress(f"stems: {t.stable_id} hydrated from fleet index")
        elif outcome.status in ("unavailable", "error"):
            progress(
                f"stems: {t.stable_id} index entry but hydrate failed: "
                f"{outcome.reason or outcome.status}"
            )
    for t in report.active():
        if not t.registered:
            continue
        manifest = json.loads(
            (stems_root / t.stable_id / "manifest.json").read_text(encoding="utf-8"))
        for part in ("vocals", "instrumental"):
            src = stems_root / t.stable_id / manifest["files"][part]
            link = stems_dir / f"{t.track_id}-{part}{src.suffix.lower()}"
            if not link.is_symlink() and not link.exists():
                link.symlink_to(src)
    to_separate = [t for t in report.active()
                   if not t.registered and t.audio_path is not None]
    if to_separate:
        inputs = [str(cmds.dir / "audio" / f"{t.track_id}{t.audio_path.suffix.lower()}")
                  for t in to_separate if t.audio_path is not None]
        progress(f"stems: separating {len(to_separate)} track(s) on Modal")
        stems_error: CommandFailed | None = None
        try:
            runner(cmds.stems(inputs))
        except CommandFailed as exc:
            stems_error = exc
        produced = 0
        for t in to_separate:
            if any(stems_dir.glob(f"{t.track_id}-vocals.*")):
                produced += 1
            else:
                report.exclude(t.track_id,
                               "stems: stage produced no pair (policy exclusion "
                               "or failure - see the separate stdout)")
        # Zero produced is only a whole-stage blocker when the corpus holds NO
        # stems at all - on a resume, registered-bundle tracks are already
        # symlinked in and the run must continue past known-hopeless stragglers.
        if produced == 0 and not any(stems_dir.glob("*-vocals.*")):
            raise StageBlocked(
                f"stems: nothing produced for {len(to_separate)} track(s)"
                + (f" - {stems_error}" if stems_error else ""))
    report.stages.append("stems")
    _register_pairs(stems_dir, report, progress, stems_root)
    report.stages.append("register")

    runner(cmds.coverage())
    report.stages.append("coverage")


def _register_pairs(stems_dir: Path, report: BatchReport, progress: Progress,
                    stems_root: Path) -> None:
    """register-before-anything-else (the batch-1 sub-rung-A lesson): every
    pair in the corpus stems dir must hold a canonical bundle before any
    later stage runs; a failure here ABORTS, never quietly excludes."""
    plan_by_id = {t.track_id: t for t in report.tracks}
    pairs = _corpus_pairs(stems_dir)
    registered_now = 0
    for key, (voc, ins) in sorted(pairs.items()):
        plan = plan_by_id.get(key)
        if plan is None:
            raise StageBlocked(f"register: stray stem pair {key!r} matches no "
                               f"staged track")
        if (stems_root / plan.stable_id / "manifest.json").is_file():
            continue
        model_name, model_version, meta_src = _meta_model(stems_dir, key)
        source_path = meta_src if meta_src != "unknown" else str(plan.audio_path)
        try:
            register_pair(stable_id=plan.stable_id, vocals=voc, instrumental=ins,
                          model_name=model_name, model_version=model_version,
                          source_path=source_path,
                          storage=RegisterPairStorage(root=stems_root))
        except Exception as exc:
            raise StageBlocked(f"register: {key} ({plan.stable_id}): {exc}") from exc
        registered_now += 1
    unregistered = [k for k in pairs
                    if not (stems_root / plan_by_id[k].stable_id /
                            "manifest.json").is_file()]
    if unregistered:
        raise StageBlocked(f"register: pairs left unregistered after the sweep: "
                           f"{unregistered}")
    progress(f"register: {registered_now} new bundle(s), "
             f"{len(pairs) - registered_now} already canonical")


def _lane_candidates(cmds: Cmds, report: BatchReport, runner: Runner,
                     progress: Progress) -> None:
    """LRCLIB candidates (+ Musixmatch tiers when the key is present) -> pin."""
    runner(cmds.spike("candidates"))
    if os.environ.get(MUSIXMATCH_ENV_KEY):
        progress("candidates: MUSIXMATCH_API_KEY present - running the "
                 "licensed tiers (strict, merge, relaxed, search)")
        runner(cmds.spike("musixmatch"))
        runner(cmds.merge_musixmatch())
        runner(cmds.spike("musixmatch-relaxed"))
        runner(cmds.spike("musixmatch-search"))
    else:
        progress("candidates: MUSIXMATCH_API_KEY absent - LRCLIB-only "
                 "candidate sets this run")
    report.stages.append("candidates")
    runner(cmds.language())
    report.stages.append("language")


def _stage_asr(cmds: Cmds, report: BatchReport, runner: Runner,
               progress: Progress) -> None:
    runner(cmds.spike("asr-jobs"))
    asr_error: CommandFailed | None = None
    try:
        runner(cmds.asr())
    except CommandFailed as exc:
        asr_error = exc
    asr_dir = cmds.dir / ASR_DIR_NAME
    stems_dir = cmds.dir / "stems"
    with_stem = [t for t in report.active()
                 if any(stems_dir.glob(f"{t.track_id}-vocals.*"))]
    transcribed = 0
    for t in with_stem:
        if (asr_dir / f"{t.track_id}.json").is_file():
            transcribed += 1
        else:
            report.exclude(t.track_id, "asr: no transcript produced")
    if with_stem and transcribed == 0:
        raise StageBlocked(f"asr: nothing transcribed for {len(with_stem)} "
                           f"stemmed track(s)"
                           + (f" - {asr_error}" if asr_error else ""))
    progress(f"asr: {transcribed}/{len(with_stem)} stemmed tracks transcribed "
             f"(language-pinned, vad_filter off)")
    report.stages.append("asr")


def _stage_align_chain(cmds: Cmds, report: BatchReport, runner: Runner,
                       progress: Progress) -> Path:
    """version screen -> language re-pin -> make-jobs -> align. Returns the
    jobs manifest path the bench manifest must read (the aligned subset)."""
    runner(cmds.spike("version-screen", "--asr", ASR_DIR_NAME))
    report.stages.append("screen")
    # Re-pin AFTER the screen so make-jobs' language/candidate assertion holds
    # (lingua must have read the same candidate the job aligns).
    runner(cmds.language())
    report.stages.append("language-repin")
    runner(cmds.spike("make-jobs"))
    jobs_path = cmds.dir / "jobs.json"
    if not jobs_path.is_file():
        raise StageBlocked(f"make-jobs: {jobs_path} was not written")
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    job_names = {j["name"] for j in jobs}
    for t in report.active():
        if t.track_id not in job_names:
            report.exclude(t.track_id, "make-jobs: excluded (no-text/undecided/"
                                       "instrumental - see the stage output)")
    if not jobs:
        raise StageBlocked("make-jobs: zero alignable tracks after exclusions")
    report.stages.append("make-jobs")

    progress(f"align: {len(jobs)} job(s) on Modal MMS")
    align_error: CommandFailed | None = None
    try:
        runner(cmds.align())
    except CommandFailed as exc:
        align_error = exc
    pred_dir = cmds.dir / PRED_DIR_NAME
    aligned_jobs = [j for j in jobs if (pred_dir / f"{j['name']}.json").is_file()]
    for j in jobs:
        if (pred_dir / f"{j['name']}.json").is_file():
            report.aligned.append(j["name"])
        else:
            report.exclude(j["name"], "align: no prediction produced")
    if not aligned_jobs:
        raise StageBlocked(f"align: nothing aligned for {len(jobs)} job(s)"
                           + (f" - {align_error}" if align_error else ""))
    report.stages.append("align")
    if len(aligned_jobs) == len(jobs):
        return jobs_path
    # The manifest generator fail-fasts on a job with no prediction, so it
    # reads the aligned SUBSET - byte-identical entries, never a regenerated
    # jobs.json (the batch-2 word-count-drift lesson).
    subset_path = cmds.dir / "jobs-aligned.json"
    subset_path.write_text(json.dumps(aligned_jobs, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    return subset_path


def _count_verdicts(state_db: Path) -> int:
    conn = state_db_mod.open_ro(state_db)
    try:
        return sum(store.count_verdicts(conn).values())
    finally:
        conn.close()


def _preflight_cloud_policies(paths: BatchPaths, stable_ids: list[str]) -> None:
    """Stage 0: refuse cloud runs when sync_policies rows are missing."""
    if policy.CFG.mode != "cloud":
        return
    conn = state_db_mod.open_rw(paths.state_db)
    try:
        machine_id = sync_stamp.ensure_local_machine(conn)
        first_sid = stable_ids[0]
        for asset_kind in ("karaoke_words", "stem_bundle"):
            try:
                resolve_policy(conn, first_sid, machine_id, asset_kind=asset_kind)
            except HydrationError as exc:
                raise StageBlocked(str(exc)) from exc
    finally:
        conn.close()


def _stage_ingest(cmds: Cmds, report: BatchReport, paths: BatchPaths,
                  progress: Progress) -> None:
    manifest_path = cmds.manifest_out()
    if not manifest_path.is_file():
        raise StageBlocked(f"ingest: manifest missing at {manifest_path}")
    before = _count_verdicts(paths.state_db)
    s3, cfg = asset_clients_for_mode(writing=True)
    report_in = ingest_state(
        manifest=manifest_path,
        coverage=cmds.dir / "vocal-presence.json",
        write=True,
        match_by="file-path",
        db_path=paths.state_db,
        s3=s3,
        cfg=cfg,
    )
    from apps.lyrics.ingest_state import format_report

    progress(format_report(report_in, manifest=manifest_path, write=True))
    if report_in.failures:
        raise StageBlocked(f"ingest: {len(report_in.failures)} track(s) failed")
    report.ingested = _count_verdicts(paths.state_db) - before
    progress(f"ingest: +{report.ingested} lyric_verdict rows "
             f"(denominator: {len(report.aligned)} aligned)")
    report.stages.append("ingest")


def _ledger_stub(report: BatchReport, paths: BatchPaths,
                 progress: Progress) -> None:
    conn = state_db_mod.open_ro(paths.state_db)
    try:
        total = sum(store.count_verdicts(conn).values())
    finally:
        conn.close()
    progress(
        "ledger: NOT appended (a human/agent stamps snapshots). Exact command:\n"
        f"  uv run {KPI_APPEND_SCRIPT} --label {report.corpus} "
        f"--provenance measured --note 'batch driver {report.corpus}' "
        f"--set library_lyric_tracks={total}\n"
        "  (add --set library_lyric_playable=<N> only from a fresh HEAD /audio "
        "probe of the live daemon - honest-denominators rule)")
    report.stages.append("ledger-stub")


#-----------------------------------------------------------------------------
# entrypoint
#-----------------------------------------------------------------------------


def _print_dry_run(cmds: Cmds, report: BatchReport) -> None:
    print(f"[DRY-RUN] batch {report.corpus}: {len(report.tracks)} track(s) resolved")
    for t in report.tracks:
        status = report.excluded.get(
            t.track_id,
            "registered bundle (skip separation)" if t.registered else "separate + register")
        print(f"  {t.track_id}  {t.stable_id}  {status}")
        print(f"      audio: {t.audio_path or 'MISSING'}")
    to_separate = [t for t in report.active() if not t.registered]
    inputs = [str(cmds.dir / "audio" / f"{t.track_id}{t.audio_path.suffix.lower()}")
              for t in to_separate if t.audio_path is not None]
    plan: list[list[str]] = [cmds.metadata()]
    if inputs:
        plan.append(cmds.stems(inputs))
    plan.append(cmds.coverage())
    plan.append(cmds.spike("candidates"))
    if os.environ.get(MUSIXMATCH_ENV_KEY):
        plan += [cmds.spike("musixmatch"), cmds.merge_musixmatch(),
                 cmds.spike("musixmatch-relaxed"), cmds.spike("musixmatch-search")]
    else:
        print("[..] MUSIXMATCH_API_KEY absent - the plan is LRCLIB-only")
    plan += [cmds.language(), cmds.spike("asr-jobs"), cmds.asr(),
             cmds.spike("version-screen", "--asr", ASR_DIR_NAME), cmds.language(),
             cmds.spike("make-jobs"), cmds.align(),
             cmds.manifest(cmds.dir / "jobs.json")]
    print("[DRY-RUN] stage commands, in order (stems and candidates lanes "
          "overlap when --live):")
    for c in plan:
        print(f"  $ {' '.join(c)}")
    print("[DRY-RUN] then in-process: register_pair per new stem pair, "
          "ingest-state --write --match-by file-path, ledger-append stub.")
    print("[DRY-RUN] nothing executed; re-run with --live")


def run_batch(
    *,
    corpus: str,
    stable_ids: list[str],
    live: bool,
    runner: Runner = run_subprocess,
    progress: Progress | None = None,
    paths: BatchPaths | None = None,
    repo_root: Path = REPO_ROOT,
) -> BatchReport:
    """The R1 driver. Raises StageBlocked when a whole stage is impossible."""
    paths = paths or BatchPaths()
    notify: Progress = progress or (lambda msg: print(f"[batch] {msg}"))
    effective_runner = (
        _subprocess_runner_for(paths) if runner is run_subprocess else runner
    )
    report = BatchReport(corpus=corpus, live=live)
    cmds = Cmds(corpus, paths.eval_dir / corpus, paths.bench_dir,
                paths.state_db.parent)
    _assert_iso3_pins(repo_root)
    _resolve_tracks(corpus, stable_ids, paths, report)

    if not live:
        _print_dry_run(cmds, report)
        return report

    _preflight_cloud_policies(paths, stable_ids)
    _preflight_r2_credentials()
    _stage_corpus(cmds, report, effective_runner, notify)
    # R2: the two lanes have independent inputs - overlap them, nothing fancier.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut_stems = pool.submit(_lane_stems, cmds, report, effective_runner,
                                notify, paths)
        fut_cands = pool.submit(_lane_candidates, cmds, report, effective_runner,
                                notify)
        concurrent.futures.wait([fut_stems, fut_cands])
    for fut in (fut_stems, fut_cands):
        exc = fut.exception()
        if exc is not None:
            raise exc
    _stage_asr(cmds, report, effective_runner, notify)
    manifest_jobs = _stage_align_chain(cmds, report, effective_runner, notify)
    effective_runner(cmds.manifest(manifest_jobs))
    report.stages.append("manifest")
    _stage_ingest(cmds, report, paths, notify)
    _ledger_stub(report, paths, notify)
    notify(report.summary())
    return report
