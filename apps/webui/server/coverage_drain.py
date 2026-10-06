"""Auto-drain: run missing vocals, lyrics and analysis work until green.

While coverage over ``present`` tracks is incomplete, the engine runs the
missing work itself, ONE job at a time, off the request thread:

1. vocals, derived on the CPU from a stem bundle that already exists
   (``apps.vocals.from_stems``), then
2. lyrics (``apps.lyrics.service.LyricsFetchService``), then
3. analysis (``coverage_drain_analysis``): one track, one worker, lowest
   priority, recently loaded tracks first. Last because it is the long pole
   (about 13 s and 2 GB a track) and the first two are seconds each and
   visible at once; it also yields to user-ordered jobs.

Stems are NEVER generated here. torch/demucs do not enter the repo venv, so a
track with no stem bundle anywhere is only REPORTED as needing the remote farm
(``status().stems_needing_farm``). A bundle evicted to R2 is not in that list:
it is done (HEALTH-07). Its vocals are derived by fetching ONE bundle at a
time (``coverage_cloud_vocals``, HEALTH-08), after lyrics and before analysis.
Tracks that can never have stems are marked finished by a cheap check
(``coverage_stems_terminal``, HEALTH-09) that runs before any job.

The setting has a whole-drain switch and one switch per step
(``coverage_drain_state.DrainConfig``, HEALTH-10), and analysis also waits
while memory pressure is high (``coverage_memory``).

The drain reads the same snapshot GET /ingest/coverage serves
(``routes.ingest.build_snapshot``), so "what is left" and "what the lights
show" are one measurement.

``MUSIC_DJ_COVERAGE_DRAIN`` is a fail-fast enum, ``on`` or ``off``, default
on for the real daemon; ``create_app`` leaves the drain unarmed so pytest
does not spawn it. The user setting (default on) lives in
``<data_dir>/state/coverage-drain.json``.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 never competes with playback
    [if] any deck is playing [then] the tick runs no job and reads no snapshot
    [if] playback starts between jobs [then] the next tick pauses
  ✔︎ never competes with the launch (PERF-BOOT-01)
    [if] the engine boot grace holds and no index was served [then] the tick takes no snapshot
    [if] the first /tracks/index is served, or the grace times out [then] the next tick runs
  ✔︎ ✅ 🎯 one job per tick, vocals before lyrics, no stems
    [if] vocals work is attemptable [then] lyrics does not run that tick
    [if] a track has no stem bundle [then] it is reported, never run
  ✔︎ ✅ 🎯 an unchanged failure is never retried in a loop
    [if] a job fails [then] it backs off 1x, 2x the base, terminal after 3
    [if] a job returns without producing its artifact [then] that is a failure
    [if] the audio file changes [then] the track is tried again
  ✔︎ ✅ 🎯 stops when green (overshoot control)
    [if] coverage is already green [then] the tick runs nothing
  ✔︎ ✅ 🎯 analysis is drained last, one at a time, and politely (HEALTH-06)
    [if] vocals or lyrics work is attemptable [then] analysis does not run
    [if] a user-ordered job is running [then] analysis yields, others do not
    [if] an analysis job says the MACHINE cannot run it [then] the step is
      unavailable until retry, and no track's attempts are spent
    [if] analysis is pending and this drain has an analysis job [then] not green
  ✔︎ ✅ 🎯 cloud vocals, terminal stems, step switches (HEALTH-07..10)
    [if] vocals are missing and the bundle is only in R2 [then] one bundle is
      fetched per job, after lyrics; never more than the transient cap held
    [if] the stem cache has no room above its floor [then] no bundle is fetched
    [if] a hub failure stops a fetch [then] no attempt is spent on the track
    [if] a pending-stems track is itself a stem, unreadable or too short
    [then] it is marked ``no_source`` before any job runs
    [if] a step is switched off [then] it never runs; the others still do
    [if] memory pressure is high [then] analysis waits, vocals and lyrics run
  ✔︎ ✅ 🎯 agent parity
    [if] start / stop / status / setting [then] HTTP route and CLI verb exist
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from apps.webui.server import coverage_cloud_vocals as cloud_vocals_mod
from apps.webui.server import coverage_drain_analysis as analysis_step
from apps.webui.server import coverage_outcomes as outcomes_mod
from apps.webui.server.coverage_drain_analysis import (
    ANALYSIS_NICENESS,
    LOAD_SETTLE_S,
    AnalysisPolicy,
    DeckGate,
    StepUnavailable,
    analysis_argv,
    analysis_job,
)
from apps.webui.server.coverage_drain_jobs import (
    JobFn,
    NoSource,
    lyrics_job,
    vocals_capability_refusal,
    vocals_job,
)
from apps.webui.server.boot_grace import NO_GRACE, BootGrace
from apps.webui.server.coverage_drain_state import DrainConfig, DrainStatus, config_path
from apps.webui.server.coverage_stems_terminal import StemsCheck
from apps.webui.server.routes.ingest_coverage import CoverageSnapshot, Target

log = logging.getLogger(__name__)

COVERAGE_DRAIN_ENV: str = "MUSIC_DJ_COVERAGE_DRAIN"
COVERAGE_DRAIN_VALUES: tuple[str, ...] = ("on", "off")
THREAD_NAME: str = "webui.coverage-drain"
#: Seconds between ticks while there is work, and while there is none. A tick
#: with nothing to do still costs one coverage snapshot, hence the long idle.
ACTIVE_INTERVAL_S: float = 1.0
IDLE_INTERVAL_S: float = 60.0
PAUSED_INTERVAL_S: float = 5.0
STOP_JOIN_S: float = 5.0
#: Ids listed in a status response; the count is always exact.
STATUS_ID_LIMIT: int = 200
#: Job order. Stems is deliberately absent: see the module docstring.
JOB_ORDER: tuple[str, ...] = ("vocals", "lyrics", "analysis")
#: Steps that wait while a user-ordered job runs or memory pressure is high.
YIELDING_STEPS: frozenset[str] = frozenset({"analysis"})
#: Pending-stems tracks classified per tick by the terminal check (an ffprobe
#: each, about 50 ms), so a 600-track backlog clears in half a minute.
STEMS_CHECK_BATCH: int = 20
#: The tick's state while the engine's boot grace holds it (PERF-BOOT-01).
STARTUP_GRACE_STATE: str = "paused_startup"

def build_for_app(app: Any) -> CoverageDrain:
    """The engine's drain (``coverage_drain_wiring``), kept importable here."""
    from apps.webui.server.coverage_drain_wiring import build_for_app as build

    return build(app)


def arm_from_environ(environ: Mapping[str, str]) -> bool:
    raw = environ.get(COVERAGE_DRAIN_ENV, "on").strip().lower()
    if raw not in COVERAGE_DRAIN_VALUES:
        raise ValueError(
            f"{COVERAGE_DRAIN_ENV}={raw!r} is not a member of {COVERAGE_DRAIN_VALUES}"
        )
    return raw == "on"


#-----------------------------------------------------------------------------
# the driver
#-----------------------------------------------------------------------------
class CoverageDrain:
    def __init__(  # noqa: PLR0913 - each argument is one independent collaborator
        self,
        *,
        snapshot_fn: Callable[[], CoverageSnapshot],
        jobs: Mapping[str, JobFn],
        playing_fn: Callable[[], bool],
        outcomes: outcomes_mod.OutcomeStore,
        config: DrainConfig,
        clock: Callable[[], float] = time.time,
        unavailable_steps: Mapping[str, str] | None = None,
        on_change: Callable[[str, str], None] | None = None,
        cloud_vocals: cloud_vocals_mod.CloudVocals | None = None,
        stems_check: StemsCheck | None = None,
        boot_grace: BootGrace = NO_GRACE,
    ) -> None:
        unknown = set(jobs) - set(JOB_ORDER)
        if unknown:
            raise ValueError(f"the drain runs only {JOB_ORDER}; refused {sorted(unknown)}")
        self._snapshot_fn = snapshot_fn
        self._jobs = dict(jobs)
        self._playing_fn = playing_fn
        self._outcomes = outcomes
        self._config = config
        self._clock = clock
        self._on_change = on_change
        self._cloud_vocals = cloud_vocals
        self._stems_check = stems_check
        self._boot_grace = boot_grace
        #: Vocals targets of this tick that need their bundle fetched first.
        self._cloud_ids: frozenset[str] = frozenset()
        #: Set after construction (``build_for_app``); the default is inert.
        self.analysis_policy = AnalysisPolicy()
        #: Steps the install cannot run at all, fixed at build time.
        self._built_unavailable: dict[str, str] = dict(unavailable_steps or {})
        self._status = DrainStatus(unavailable_steps=dict(self._built_unavailable))
        self._stopped = False
        #: The snapshot taken to verify the last job, reused once as the next
        #: tick's input so a run of jobs costs one snapshot each, not two.
        self._carried: CoverageSnapshot | None = None
        self._tick_lock = threading.Lock()
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def job_steps(self) -> tuple[str, ...]:
        return tuple(step for step in JOB_ORDER if step in self._jobs)

    # --- control ----------------------------------------------------------
    def request_stop(self) -> None:
        self._stopped = True
        self._status.stop_requested = True
        self._status.state = "stopped"

    def request_start(self) -> None:
        self._stopped = False
        self._status.stop_requested = False
        self._status.state = "idle"
        self._wake.set()

    @property
    def outcomes(self) -> outcomes_mod.OutcomeStore:
        """The ledger this drain writes; other writers share it for its lock."""
        return self._outcomes

    def set_enabled(self, enabled: bool) -> None:
        self.update_config(enabled=enabled)

    def update_config(self, **changes: Any) -> None:
        self._config.update(**changes)
        self.invalidate()

    def invalidate(self) -> None:
        """Drop the carried snapshot and re-check now (config or marks moved)."""
        self._carried = None
        self._wake.set()

    def retry_failed(self) -> int:
        """Re-arm every failed track. Returns how many were re-armed."""
        cleared = self._outcomes.clear_failures()
        # A step a job declared unavailable at run time gets another try too.
        self._status.unavailable_steps = dict(self._built_unavailable)
        self._carried = None
        self._wake.set()
        return cleared

    def status(self) -> DrainStatus:
        self._status.enabled = self._config.enabled()
        self._status.steps_enabled = self._config.steps()
        return self._status

    # --- one tick ---------------------------------------------------------
    def tick(self) -> str:
        with self._tick_lock:
            outcome = self._tick()
            self._status.ticks += 1
            self._status.updated_at = self._clock()
            return outcome

    def _settle(self, state: str, reason: str | None = None) -> str:
        self._status.state = state
        self._status.reason = reason
        return state

    def _tick(self) -> str:
        if (idle := self._idle_reason()) is not None:
            return self._settle(idle)
        snapshot, self._carried = self._carried or self._snapshot_fn(), None
        self._absorb(snapshot)
        steps_on = self._config.steps()
        if self._green(snapshot, steps_on):
            return self._settle("green")
        if self._check_stems(snapshot):
            return self._settle("ran:stems-check")

        now = self._clock()
        ledger = self._outcomes.load()
        retry_times: list[float] = []
        for step, targets in self._phases(snapshot):
            if not self._step_may_run(step, steps_on):
                continue
            for stable_id, audio_path in targets:
                signature = outcomes_mod.audio_token(Path(audio_path))
                prior = ledger.get((step, stable_id))
                if outcomes_mod.may_attempt(prior, signature, now=now):
                    return self._run_unless_yielding(step, stable_id, audio_path, signature)
                if prior is not None and prior.attempts < outcomes_mod.MAX_ATTEMPTS:
                    retry_times.append(outcomes_mod.next_attempt_at(prior))
        return self._settle_blocked(snapshot, retry_times)

    def _run_unless_yielding(
        self, step: str, stable_id: str, audio_path: str, signature: str
    ) -> str:
        if step in YIELDING_STEPS and (held := self._yield_reason(step)):
            return self._settle(*held)
        return self._run(step, stable_id, audio_path, signature)

    def _settle_blocked(self, snapshot: CoverageSnapshot, retry_times: list[float]) -> str:
        self._status.next_retry_at = min(retry_times) if retry_times else None
        return self._settle("blocked", self._blocked_reason(snapshot, bool(retry_times)))

    def _idle_reason(self) -> str | None:
        """Why this tick does no work before measuring anything, or None."""
        if not self._config.enabled():
            return "disabled"
        if self._stopped:
            return "stopped"
        if self._playing_fn():
            return "paused_playing"
        if self._boot_grace.active():
            return STARTUP_GRACE_STATE  # PERF-BOOT-01: the launch's index goes first
        return None

    def _phases(self, snapshot: CoverageSnapshot) -> list[tuple[str, Sequence[Target]]]:
        """Each step with its targets, in the order the drain works them."""
        cloud_targets = self._cloud_targets(snapshot)
        self._cloud_ids = frozenset(sid for sid, _path in cloud_targets)
        # Bundles are fetched for vocals only after lyrics: a fetch is seconds
        # and megabytes, and the quick, visible work should not wait on it.
        phases = [(step, self._targets(step, snapshot)) for step in JOB_ORDER]
        phases.insert(JOB_ORDER.index("lyrics") + 1, ("vocals", cloud_targets))
        return phases

    def _step_may_run(self, step: str, steps_on: Mapping[str, bool]) -> bool:
        return (
            step in self._jobs
            and step not in self._status.unavailable_steps
            and steps_on.get(step, True)
        )

    def _green(self, snapshot: CoverageSnapshot, steps_on: Mapping[str, bool]) -> bool:
        """Done: the lights' own rule, plus analysis when this drain owns it."""
        counts = snapshot.counts["analysis"]
        owns_analysis = "analysis" in self._jobs and steps_on.get("analysis", True)
        return snapshot.green and not (owns_analysis and (counts.pending or counts.failed))

    def _yield_reason(self, step: str) -> tuple[str, str] | None:
        """(state, reason) when a yielding step must wait this tick."""
        if self.analysis_policy.user_jobs_fn():
            return "yielding_user_jobs", f"{step} waits while a user-ordered job runs"
        self._status.memory_pressure = self.analysis_policy.memory_pressure_fn()
        if self._status.memory_pressure is not None:
            return "yielding_memory_pressure", f"{step} waits: {self._status.memory_pressure}"
        return None

    def _check_stems(self, snapshot: CoverageSnapshot) -> bool:
        """Mark pending-stems tracks that can never have stems. True if it ran."""
        if self._stems_check is None or snapshot.stem_cloud.state == "unknown":
            return False    # unknown: "pending" is unclassified, not checkable
        report = self._stems_check.run(snapshot.pending["stems"], limit=STEMS_CHECK_BATCH)
        self._status.stems_check = {
            "marked_total": self._stems_check.marked_total, "last_marked": list(report.marked),
        }
        if report.marked and self._on_change is not None:
            self._on_change("stems", report.marked[0])
        return report.checked > 0

    def _cloud_targets(self, snapshot: CoverageSnapshot) -> Sequence[Target]:
        """Vocals targets whose bundle may be fetched from R2 this tick."""
        cloud = self._cloud_vocals
        info: dict[str, Any] = {"pending": len(snapshot.vocals_cloud_ready), "paused_reason": None}
        targets: Sequence[Target] = ()
        if cloud is None:
            info["paused_reason"] = "this drain has no cloud vocals runner"
        elif snapshot.vocals_cloud_ready:
            info["paused_reason"] = cloud.refusal()
            if info["paused_reason"] is None:
                targets = snapshot.vocals_cloud_ready
        if cloud is not None:
            info.update(cloud.status())
        self._status.cloud_vocals = info
        return targets

    def _targets(self, step: str, snapshot: CoverageSnapshot) -> Sequence[Target]:
        if step == "vocals":
            return snapshot.vocals_ready
        if step == "analysis":
            return analysis_step.recent_first(
                snapshot.pending[step], self.analysis_policy.recency_fn()
            )
        return snapshot.pending[step]

    def _absorb(self, snapshot: CoverageSnapshot) -> None:
        status = self._status
        policy = self.analysis_policy
        status.farm_only_stages = dict(policy.farm_only_stages)
        if policy.farm_pending_fn is not None:
            status.farm_only_pending = policy.farm_pending_fn(snapshot.on_disk)
        status.pending = {step: counts.pending for step, counts in snapshot.counts.items()}
        status.failed = {step: counts.failed for step, counts in snapshot.counts.items()}
        status.waiting_on_stems = snapshot.waiting_on_stems
        cloud = snapshot.stem_cloud
        status.stems_index_state, status.stems_index_reason = cloud.state, cloud.reason
        status.stems_in_cloud = snapshot.stems_in_cloud
        status.stems_no_source = snapshot.counts["stems"].terminal
        # With the index unreadable a missing bundle is unclassified: it is
        # not known to need the farm, and must not be reported as if it did.
        missing = [stable_id for stable_id, _path in snapshot.pending["stems"]]
        farm = [] if cloud.state == "unknown" else missing
        status.stems_unclassified = len(missing) - len(farm)
        status.stems_needing_farm = farm[:STATUS_ID_LIMIT]
        status.stems_needing_farm_count = len(farm)
        status.memory_pressure = None
        status.next_retry_at = None

    def _blocked_reason(self, snapshot: CoverageSnapshot, retrying: bool) -> str:
        counts = snapshot.counts
        parts: list[str] = ["a failed job is waiting out its retry backoff"] if retrying else []
        parts.extend(self._stems_blocked_parts(snapshot))
        for step, why in self._status.unavailable_steps.items():
            if counts[step].pending:
                parts.append(f"{step} cannot run here: {why}")
        for step, on in self._config.steps().items():
            if not on and counts[step].pending:
                parts.append(f"{step} is switched off in the drain setting")
        failed = {step: c.failed for step, c in counts.items() if c.failed}
        if failed:
            parts.append(f"jobs failed {outcomes_mod.MAX_ATTEMPTS} times and need a retry: {failed}")
        return "; ".join(parts) or "work is pending that this drain does not run"

    def _stems_blocked_parts(self, snapshot: CoverageSnapshot) -> list[str]:
        """Why stems, and the vocals that wait on a stem download, are not moving."""
        status = self._status
        parts: list[str] = []
        pending = snapshot.counts["stems"].pending
        if status.stems_unclassified:
            parts.append(
                f"{status.stems_unclassified} track(s) have no local stem bundle and "
                f"the R2 index cannot say whether one exists ({status.stems_index_reason})"
            )
        elif pending:
            parts.append(
                f"{pending} track(s) need stems from the remote farm "
                "(not generated on this machine)"
            )
        paused = status.cloud_vocals.get("paused_reason")
        if snapshot.vocals_cloud_ready and paused:
            parts.append(
                f"{len(snapshot.vocals_cloud_ready)} vocals wait on a stem download: {paused}"
            )
        return parts

    def _runner(
        self, step: str, stable_id: str
    ) -> tuple[JobFn, cloud_vocals_mod.CloudVocals | None]:
        """The job for this target, and the cloud runner when it needs a fetch."""
        cloud = self._cloud_vocals
        if step == "vocals" and cloud is not None and stable_id in self._cloud_ids:
            return cloud.run, cloud
        return self._jobs[step], None

    def _run(self, step: str, stable_id: str, audio_path: str, signature: str) -> str:
        self._status.state = "running"
        started = self._clock()
        error: str | None = None
        no_source_recorded = False
        runner, cloud = self._runner(step, stable_id)
        try:
            runner(stable_id, audio_path)
        except cloud_vocals_mod.CloudPaused as paused:
            # About the connection, not this track: spend no attempt.
            log.warning("coverage drain cloud vocals paused: %s", paused)
            return self._settle("blocked", f"cloud vocals paused: {paused}")
        except NoSource as no_source:
            self._outcomes.record_no_source(step, stable_id, signature, str(no_source), now=started)
            no_source_recorded = True
        except StepUnavailable as unavailable:
            # About this machine, not this track: stop the step, spend no attempt.
            log.warning("coverage drain %s step unavailable: %s", step, unavailable)
            self._status.unavailable_steps[step] = str(unavailable)
            return self._settle("blocked", f"{step} cannot run here: {unavailable}")
        except Exception as failure:  # noqa: BLE001 - recorded, backed off, surfaced in status
            log.warning("coverage drain %s job failed for %s: %s", step, stable_id, failure)
            error = f"{type(failure).__name__}: {failure}"
        if error is None:
            after = self._snapshot_fn()
            if any(sid == stable_id for sid, _path in after.pending[step]):
                error = "the job finished without producing its artifact"
            else:
                self._carried = after
                self._absorb(after)
        self._status.cloud_vocals.update(cloud.status() if cloud else {})   # after this fetch
        self._status.last_job = {
            "step": step,
            "stable_id": stable_id,
            "ok": error is None,
            "error": error,
            "at": started,
        }
        if error is not None:
            self._outcomes.record_failure(step, stable_id, signature, error, now=started)
            self._status.jobs_failed += 1
            return self._settle(f"failed:{step}", error)
        if not no_source_recorded:
            self._outcomes.clear(step, stable_id)   # a success retires any earlier failure
        self._status.jobs_run += 1
        if self._on_change is not None:
            self._on_change(step, stable_id)
        return self._settle(f"ran:{step}")

    # --- background loop --------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, name=THREAD_NAME, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Shut the loop down (engine exit). Not the user-facing stop verb."""
        self._halt.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=STOP_JOIN_S)
        self._thread = None

    def _loop(self) -> None:
        interval = ACTIVE_INTERVAL_S
        while not self._halt.is_set():
            self._wake.wait(interval)
            self._wake.clear()
            if self._halt.is_set():
                return
            try:
                outcome = self.tick()
            except Exception:
                log.exception("coverage drain tick failed")
                self._status.state = "blocked"
                self._status.reason = "the coverage snapshot could not be read; see the engine log"
                interval = IDLE_INTERVAL_S
                continue
            if outcome.startswith(("ran:", "failed:")):
                interval = ACTIVE_INTERVAL_S
            elif outcome in ("paused_playing", "yielding_user_jobs", STARTUP_GRACE_STATE):
                interval = PAUSED_INTERVAL_S
            else:
                interval = IDLE_INTERVAL_S

    def wake(self) -> None:
        """Ask the loop to re-check now (the library changed)."""
        self._wake.set()


__all__ = [
    "ANALYSIS_NICENESS",
    "COVERAGE_DRAIN_ENV",
    "LOAD_SETTLE_S",
    "STARTUP_GRACE_STATE",
    "AnalysisPolicy",
    "CoverageDrain",
    "DeckGate",
    "DrainConfig",
    "DrainStatus",
    "JobFn",
    "NoSource",
    "StepUnavailable",
    "analysis_argv",
    "analysis_job",
    "arm_from_environ",
    "build_for_app",
    "config_path",
    "lyrics_job",
    "vocals_capability_refusal",
    "vocals_job",
]
