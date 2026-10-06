"""The auto-drain's analysis step: the job, its gates, and what it leaves alone.

Split out of ``coverage_drain`` (file-size ceiling) along a real seam: that
module is the driver; this one is everything specific to analysis.

The job runs what "Refresh analysis" runs (``apps.analysis.run`` on the
coverage backend, so the drain and the analysis light measure one thing), for
ONE track, with ONE worker, in a child at the lowest scheduling priority. A
4-worker backfill swap-thrashed this class of laptop; one librosa job peaks at
about 1.5 to 2.5 GB resident (measured Thu 1 Oct 2026), so one is the ceiling.

Stages that need torch are never run here. The own beatgrid producer
(``own_beatgrid.backfill``, Beat This!) is the one that writes the
constant-tempo grid, and it is only REPORTED (``farm_only_stages``): heavy ML
dependencies do not enter the engine environment.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 an exit code about the machine never burns per-track attempts
    [if] the CLI exits 1 or 3 [then] that track failed: RuntimeError
    [if] the CLI exits anything else non-zero [then ⛔️] StepUnavailable
  ✔︎ ✅ 🎯 nothing starts while a deck is playing or loading
    [if] a deck reports playing [then] the gate holds
    [if] a deck took a track it did not hold before [then] the gate holds for LOAD_SETTLE_S
    [if] decks are loaded and stopped [then] the gate does not hold (DRAIN-PAUSE-01)
  ✔︎ ✅ user-ordered work goes first
    [if] the refresh job or any queue item is running [then] analysis yields
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apps.analysis import backends as analysis_backends
from apps.analysis import run as analysis_run
from apps.analysis_beatgrid.version import PRODUCER_VERSION as BEATGRID_PRODUCER_VERSION
from apps.shared.paths import PROJECT_ROOT
from apps.shared.sync_runtime_gates import any_deck_playing
from apps.webui.server import coverage_analysis_job

AF_SERVICE_ID: str = "com.af.music-dj-tools.coverage-drain.analysis"
#: The child's scheduling priority (see ``coverage_analysis_job``).
ANALYSIS_NICENESS: int = coverage_analysis_job.NICENESS
#: One track. A cold first job pays the JIT warmup (about 50 s measured).
ANALYSIS_JOB_TIMEOUT_S: float = 900.0
#: How long after a track lands on a deck the drain starts nothing. The UI
#: mirror carries no loading flag, so a changed deck track IS the load signal;
#: this covers decode, waveform and stem attach.
LOAD_SETTLE_S: float = 20.0
#: Exit codes that describe THIS track. Everything else describes the machine
#: (no backend, bad flags, an escaped exception) and would recur on every
#: track; the same line ``routes.ingest_job._PER_TARGET_EXITS`` draws.
PER_TRACK_EXITS: frozenset[int] = frozenset({
    analysis_run.EXIT_TRACK_FAILURES,
    analysis_run.EXIT_MISSING_TARGETS,
})
#: Analysis stages this drain reports and never runs, with the reason.
FARM_ONLY_STAGES: dict[str, str] = {
    analysis_backends.OWN_BEATGRID_BACKEND: (
        "the constant-tempo beatgrid (producer "
        f"{BEATGRID_PRODUCER_VERSION}) comes from the Beat This! tracker, which "
        "needs torch; torch never enters the engine environment, so this "
        "stage is not run on this machine by the drain"
    ),
}

JobFn = Callable[[str, str], None]
Target = tuple[str, str]


class StepUnavailable(Exception):
    """A job's answer that this MACHINE cannot run the step. Not a track failure."""


@dataclass(frozen=True)
class AnalysisPolicy:
    """How the drain's analysis step behaves. The defaults change nothing."""

    #: True while a user-ordered job is running; analysis waits.
    user_jobs_fn: Callable[[], bool] = lambda: False
    #: Why analysis should wait for memory, or None (``coverage_memory``).
    memory_pressure_fn: Callable[[], str | None] = lambda: None
    #: Recently loaded or played track ids, most recent first.
    recency_fn: Callable[[], Sequence[str]] = lambda: ()
    #: Stages reported and never run here, with the reason.
    farm_only_stages: Mapping[str, str] = field(default_factory=dict)
    #: Present targets in, per-stage count of tracks with no current record out.
    farm_pending_fn: Callable[[Sequence[Target]], dict[str, int]] | None = None


#-----------------------------------------------------------------------------
# the job
#-----------------------------------------------------------------------------
def analysis_capability_refusal() -> str | None:
    """Why this install cannot analyze at all, or None when it can."""
    if not analysis_backends.default_backend_installed():
        return (
            f"the {analysis_backends.DEFAULT_BACKEND!r} analysis backend is not "
            "installed in the engine environment (analysis extra)"
        )
    return None


def analysis_argv(stable_id: str, audio_path: str, *, backend: str) -> tuple[list[str], str]:
    """The child argv for one track, plus the pairs file the caller deletes."""
    with tempfile.NamedTemporaryFile("w", suffix=".pairs.json", delete=False) as handle:
        json.dump([[stable_id, audio_path]], handle)
    argv = [
        sys.executable, "-m", "apps.webui.server.coverage_analysis_job",
        "--backend", backend, "--workers", "1", "--pairs-json", handle.name,
    ]
    return argv, handle.name


def analysis_job(data_dir: Path, *, backend: str) -> JobFn:
    """Analyze one track in a child process at the lowest priority."""

    def run(stable_id: str, audio_path: str) -> None:
        argv, pairs_path = analysis_argv(stable_id, audio_path, backend=backend)
        try:
            completed = subprocess.run(
                argv,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=ANALYSIS_JOB_TIMEOUT_S,
                env={**os.environ, "MDT_DATA_DIR": str(data_dir), "AF_SERVICE_ID": AF_SERVICE_ID},
                check=False,
            )
        finally:
            Path(pairs_path).unlink(missing_ok=True)
        if completed.returncode == 0:
            return
        tail = " ".join((completed.stderr or completed.stdout).strip().splitlines()[-1:])
        message = f"apps.analysis.run ({backend}) exited {completed.returncode}: {tail}"
        if completed.returncode in PER_TRACK_EXITS:
            raise RuntimeError(message)
        raise StepUnavailable(message)

    return run


#-----------------------------------------------------------------------------
# gates
#-----------------------------------------------------------------------------
class DeckGate:
    """True while any deck is playing, or a NEW track landed on one moments ago.

    A loaded, stopped deck never holds (DRAIN-PAUSE-01): a DJ almost always has
    decks loaded. Only a track the deck did not hold before starts the settle
    hold, so a deck whose id blinks out of a mirror snapshot and back (a second
    publisher, a reload of the same track) is not a load each time.
    """

    def __init__(
        self,
        mirror_fn: Callable[[], Mapping[str, Any] | None],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._mirror_fn = mirror_fn
        self._clock = clock
        #: The last track each deck actually held; an empty snapshot does not clear it.
        self._loaded: dict[str, str] = {}
        self._hold_until: float = 0.0

    def __call__(self) -> bool:
        mirror = self._mirror_fn()
        now = self._clock()
        decks = (mirror or {}).get("decks")
        for name, deck in (decks.items() if isinstance(decks, dict) else ()):
            stable_id = deck.get("stable_id") if isinstance(deck, dict) else None
            if stable_id is None or self._loaded.get(name) == stable_id:
                continue
            self._hold_until = now + LOAD_SETTLE_S
            self._loaded[name] = stable_id
        return any_deck_playing(mirror) or now < self._hold_until


def user_jobs_active(
    conn_factory: Callable[[], sqlite3.Connection], refresh_running_fn: Callable[[], bool]
) -> bool:
    """A user-ordered job is running: the refresh slot, or any queue item.

    ``running`` rather than ``pending``: a pending item no runner will ever
    claim (a stems order waiting on an unreachable farm) must not park the
    drain for good. The user-lane supervisors claim a runnable item within a
    second, so the window this leaves is one tick.
    """
    if refresh_running_fn():
        return True
    conn = conn_factory()
    try:
        has_queue = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_queue_item'"
        ).fetchone()
        if has_queue is None:
            return False
        return conn.execute(
            "SELECT 1 FROM analysis_queue_item WHERE state = 'running' LIMIT 1"
        ).fetchone() is not None
    finally:
        conn.close()


#-----------------------------------------------------------------------------
# ordering and reporting
#-----------------------------------------------------------------------------
def recent_first(targets: Sequence[Target], recent_ids: Sequence[str]) -> list[Target]:
    """``targets`` with recently used tracks first, the rest in their own order."""
    rank = {stable_id: index for index, stable_id in enumerate(recent_ids)}
    return sorted(targets, key=lambda target: rank.get(target[0], len(rank)))


def farm_only_pending(
    conn_factory: Callable[[], sqlite3.Connection], present: Sequence[Target]
) -> dict[str, int]:
    """Per farm-only stage, how many present tracks have no current record."""
    conn = conn_factory()
    try:
        has_analysis = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis'"
        ).fetchone()
        current = (
            {
                row[0]
                for row in conn.execute(
                    "SELECT stable_id FROM analysis WHERE backend = ? AND backend_version = ?",
                    (analysis_backends.OWN_BEATGRID_BACKEND, BEATGRID_PRODUCER_VERSION),
                )
            }
            if has_analysis is not None
            else set()
        )
    finally:
        conn.close()
    return {
        analysis_backends.OWN_BEATGRID_BACKEND: sum(
            1 for stable_id, _path in present if stable_id not in current
        )
    }


__all__ = [
    "ANALYSIS_NICENESS",
    "FARM_ONLY_STAGES",
    "LOAD_SETTLE_S",
    "PER_TRACK_EXITS",
    "AnalysisPolicy",
    "DeckGate",
    "StepUnavailable",
    "analysis_argv",
    "analysis_capability_refusal",
    "analysis_job",
    "farm_only_pending",
    "recent_first",
    "user_jobs_active",
]
