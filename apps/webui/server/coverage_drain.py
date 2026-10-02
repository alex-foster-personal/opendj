"""Auto-drain: run missing vocals and lyrics work until the lights are green.

While coverage over ``present`` tracks is incomplete, the engine runs the
missing work itself, ONE job at a time, off the request thread:

1. vocals, derived on the CPU from a stem bundle that already exists
   (``apps.vocals.from_stems``), then
2. lyrics (``apps.lyrics.service.LyricsFetchService``).

Stems are NEVER generated here. torch/demucs do not enter the repo venv, so a
track with no stem bundle is only REPORTED as needing the remote farm
(``status().stems_needing_farm``).

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
  ✔︎ ✅ 🎯 one job per tick, vocals before lyrics, no stems
    [if] vocals work is attemptable [then] lyrics does not run that tick
    [if] a track has no stem bundle [then] it is reported, never run
  ✔︎ ✅ 🎯 an unchanged failure is never retried in a loop
    [if] a job fails [then] it backs off 1x, 2x the base, terminal after 3
    [if] a job returns without producing its artifact [then] that is a failure
    [if] the audio file changes [then] the track is tried again
  ✔︎ ✅ 🎯 stops when green (overshoot control)
    [if] coverage is already green [then] the tick runs nothing
  ✔︎ ✅ 🎯 agent parity
    [if] start / stop / status / setting [then] HTTP route and CLI verb exist
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from apps.lyrics.service import LyricsFetchService, load_track
from apps.shared.events import publish
from apps.shared.sync_runtime_gates import any_deck_playing
from apps.webui.server import coverage_outcomes as outcomes_mod
from apps.webui.server.routes.ingest_coverage import CoverageSnapshot

log = logging.getLogger(__name__)

COVERAGE_DRAIN_ENV: str = "MUSIC_DJ_COVERAGE_DRAIN"
COVERAGE_DRAIN_VALUES: tuple[str, ...] = ("on", "off")
CONFIG_FILENAME: str = "coverage-drain.json"
THREAD_NAME: str = "webui.coverage-drain"
AF_SERVICE_ID: str = "com.af.music-dj-tools.coverage-drain"
#: Seconds between ticks while there is work, and while there is none. A tick
#: with nothing to do still costs one coverage snapshot, hence the long idle.
ACTIVE_INTERVAL_S: float = 1.0
IDLE_INTERVAL_S: float = 60.0
PAUSED_INTERVAL_S: float = 5.0
VOCALS_JOB_TIMEOUT_S: float = 300.0
STOP_JOIN_S: float = 5.0
#: Ids listed in a status response; the count is always exact.
STATUS_ID_LIMIT: int = 200
#: Job order. Stems is deliberately absent: see the module docstring.
JOB_ORDER: tuple[str, ...] = ("vocals", "lyrics")

JobFn = Callable[[str, str], None]


class NoSource(Exception):
    """A job's answer that there is nothing to make for this track. Terminal."""


def arm_from_environ(environ: Mapping[str, str]) -> bool:
    raw = environ.get(COVERAGE_DRAIN_ENV, "on").strip().lower()
    if raw not in COVERAGE_DRAIN_VALUES:
        raise ValueError(
            f"{COVERAGE_DRAIN_ENV}={raw!r} is not a member of {COVERAGE_DRAIN_VALUES}"
        )
    return raw == "on"


#-----------------------------------------------------------------------------
# the setting
#-----------------------------------------------------------------------------
def config_path(data_dir: Path) -> Path:
    return data_dir / "state" / CONFIG_FILENAME


class DrainConfig:
    """The on/off setting. Absent file = on, the documented default."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def enabled(self) -> bool:
        if not self.path.is_file():
            return True
        value = json.loads(self.path.read_text(encoding="utf-8")).get("enabled")
        if not isinstance(value, bool):
            raise TypeError(f"{self.path}: 'enabled' must be true or false, got {value!r}")
        return value

    def set_enabled(self, enabled: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"enabled": enabled}) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)


#-----------------------------------------------------------------------------
# jobs
#-----------------------------------------------------------------------------
def lyrics_job(service: LyricsFetchService, runs: list[str] | None = None) -> JobFn:
    """Fetch lyrics for one track through the shared fetch/cache path.

    The service records ``instrumental`` / ``no_source`` verdicts itself; the
    coverage snapshot reads those, so they need no second record here.
    """

    def run(stable_id: str, _audio_path: str) -> None:
        if runs is not None:
            runs.append(stable_id)
        try:
            track = load_track(service.data_dir / "state" / "state.db", stable_id)
        except ValueError as error:
            # No artist, title or duration to look the track up by.
            raise NoSource(str(error)) from error
        service.fetch_or_resolve(track)

    return run


def vocals_capability_refusal() -> str | None:
    """Why this install cannot derive vocals from stems, or None when it can."""
    if importlib.util.find_spec("soundfile") is None:
        return "soundfile is not installed in the engine environment (analysis extra)"
    return None


def vocals_job(data_dir: Path, stem_roots: Sequence[Path]) -> JobFn:
    """Derive one vocal-cache entry in a niced child process.

    A child, not a thread: reading and reducing a full-length stem is CPU
    work that would otherwise hold the engine's interpreter lock.
    """

    def run(stable_id: str, audio_path: str) -> None:
        argv = [
            sys.executable, "-m", "apps.webui.server.coverage_vocals_job",
            "--data-dir", str(data_dir), "--stable-id", stable_id, "--audio-path", audio_path,
        ]
        for root in stem_roots:
            argv += ["--stem-root", str(root)]
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=VOCALS_JOB_TIMEOUT_S,
            env={**os.environ, "AF_SERVICE_ID": f"{AF_SERVICE_ID}.vocals"},
            check=False,
        )
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout).strip().splitlines()[-1:]
            raise RuntimeError(
                f"vocals from-stems exited {completed.returncode}: {' '.join(tail)}"
            )

    return run


#-----------------------------------------------------------------------------
# status
#-----------------------------------------------------------------------------
@dataclass
class DrainStatus:
    #: idle | green | running | blocked | paused_playing | stopped | disabled
    state: str = "idle"
    enabled: bool = True
    stop_requested: bool = False
    #: Plain-language cause of a ``blocked`` state; None otherwise.
    reason: str | None = None
    pending: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)
    waiting_on_stems: int = 0
    stems_needing_farm: list[str] = field(default_factory=list)
    stems_needing_farm_count: int = 0
    #: Steps this install cannot run at all, with the reason.
    unavailable_steps: dict[str, str] = field(default_factory=dict)
    next_retry_at: float | None = None
    last_job: dict[str, Any] | None = None
    jobs_run: int = 0
    jobs_failed: int = 0
    ticks: int = 0
    updated_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


#-----------------------------------------------------------------------------
# the driver
#-----------------------------------------------------------------------------
class CoverageDrain:
    def __init__(
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
        self._status = DrainStatus(unavailable_steps=dict(unavailable_steps or {}))
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

    def set_enabled(self, enabled: bool) -> None:
        self._config.set_enabled(enabled)
        self._wake.set()

    def retry_failed(self) -> int:
        """Re-arm every failed track. Returns how many were re-armed."""
        cleared = self._outcomes.clear_failures()
        self._carried = None
        self._wake.set()
        return cleared

    def status(self) -> DrainStatus:
        self._status.enabled = self._config.enabled()
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
        if snapshot.green:
            return self._settle("green")

        now = self._clock()
        ledger = self._outcomes.load()
        retry_times: list[float] = []
        for step, targets in (("vocals", snapshot.vocals_ready), ("lyrics", snapshot.pending["lyrics"])):
            if step not in self._jobs or step in self._status.unavailable_steps:
                continue
            for stable_id, audio_path in targets:
                signature = outcomes_mod.audio_token(Path(audio_path))
                prior = ledger.get((step, stable_id))
                if outcomes_mod.may_attempt(prior, signature, now=now):
                    return self._run(step, stable_id, audio_path, signature)
                if prior is not None and prior.attempts < outcomes_mod.MAX_ATTEMPTS:
                    retry_times.append(outcomes_mod.next_attempt_at(prior))
        return self._settle_blocked(snapshot, retry_times)

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
        return None

    def _absorb(self, snapshot: CoverageSnapshot) -> None:
        status = self._status
        status.pending = {step: counts.pending for step, counts in snapshot.counts.items()}
        status.failed = {step: counts.failed for step, counts in snapshot.counts.items()}
        status.waiting_on_stems = snapshot.waiting_on_stems
        farm = [stable_id for stable_id, _path in snapshot.pending["stems"]]
        status.stems_needing_farm = farm[:STATUS_ID_LIMIT]
        status.stems_needing_farm_count = len(farm)
        status.next_retry_at = None

    def _blocked_reason(self, snapshot: CoverageSnapshot, retrying: bool) -> str:
        counts = snapshot.counts
        parts: list[str] = []
        if retrying:
            parts.append("a failed job is waiting out its retry backoff")
        if counts["stems"].pending:
            parts.append(
                f"{counts['stems'].pending} track(s) need stems from the remote farm "
                "(not generated on this machine)"
            )
        for step, why in self._status.unavailable_steps.items():
            if counts[step].pending:
                parts.append(f"{step} cannot run here: {why}")
        failed = {step: c.failed for step, c in counts.items() if c.failed}
        if failed:
            parts.append(f"jobs failed {outcomes_mod.MAX_ATTEMPTS} times and need a retry: {failed}")
        return "; ".join(parts) or "work is pending that this drain does not run"

    def _run(self, step: str, stable_id: str, audio_path: str, signature: str) -> str:
        self._status.state = "running"
        started = self._clock()
        error: str | None = None
        no_source_recorded = False
        try:
            self._jobs[step](stable_id, audio_path)
        except NoSource as no_source:
            self._outcomes.record_no_source(step, stable_id, signature, str(no_source), now=started)
            no_source_recorded = True
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
            elif outcome == "paused_playing":
                interval = PAUSED_INTERVAL_S
            else:
                interval = IDLE_INTERVAL_S

    def wake(self) -> None:
        """Ask the loop to re-check now (the library changed)."""
        self._wake.set()


#-----------------------------------------------------------------------------
# wiring
#-----------------------------------------------------------------------------
def build_for_app(app: FastAPI) -> CoverageDrain:
    """The engine's drain: real jobs, the app's own snapshot and deck mirror."""
    from apps.webui.server.routes import ingest as ingest_routes

    data_dir = ingest_routes.COVERAGE_DATA_DIR
    refusal = vocals_capability_refusal()

    def changed(step: str, stable_id: str) -> None:
        # ``library_jobs`` is the kind the user-ordered stems/lyrics lanes
        # already publish for a finished per-track artifact.
        _ = step
        publish("library.changed", {"kind": "library_jobs", "ids": [stable_id]})

    return CoverageDrain(
        snapshot_fn=lambda: ingest_routes.build_snapshot(app),
        jobs={
            "vocals": vocals_job(data_dir, ingest_routes._stem_roots(app)),
            "lyrics": lyrics_job(LyricsFetchService(data_dir)),
        },
        playing_fn=lambda: any_deck_playing(getattr(app.state, "ui_mirror", None)),
        outcomes=outcomes_mod.OutcomeStore(outcomes_mod.store_path(data_dir)),
        config=DrainConfig(config_path(data_dir)),
        unavailable_steps={"vocals": refusal} if refusal is not None else None,
        on_change=changed,
    )


__all__ = [
    "COVERAGE_DRAIN_ENV",
    "CoverageDrain",
    "DrainConfig",
    "DrainStatus",
    "NoSource",
    "arm_from_environ",
    "build_for_app",
    "config_path",
    "lyrics_job",
    "vocals_capability_refusal",
    "vocals_job",
]
