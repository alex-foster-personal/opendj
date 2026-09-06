"""The refresh job itself: its state, its one slot, and how it runs a CLI.

Split out of routes/ingest.py, which owns the ROUTES. This module owns the
job those routes start: the record a worker mutates, the single-slot registry
that makes a second concurrent refresh a 409, and the subprocess runner every
step goes through.

The split is a size limit made useful. ingest.py sat at exactly the 600-line
ceiling, so the analysis step could not grow a capability-failure branch
without breaking it, and these four things are the part with no route in
them: nothing here imports FastAPI or touches a request.

Import direction is one-way - ingest.py imports from here, never the reverse -
which is also why ``_RefreshJob`` moved rather than being type-imported: a
TYPE_CHECKING import back into ingest.py would be a cycle wearing a hat.
"""
from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from apps.analysis import run as analysis_run
from apps.shared.paths import PROJECT_ROOT
from apps.webui.server.routes.ingest_analysis_argv import CliFailed

#: Exit codes that describe THESE targets rather than this machine. Only
#: these may leave the chunk loop running; see :func:`_step_analysis`. The
#: missing-target code is here because that run still analyzed everything it
#: did admit - it reports a shortfall, it does not report a wall.
_PER_TARGET_EXITS: frozenset[int] = frozenset({
    analysis_run.EXIT_TRACK_FAILURES,
    analysis_run.EXIT_MISSING_TARGETS,
})


def _systemic_message(exc: CliFailed, total: int, backend: str) -> str:
    """Why the whole step stopped, in terms a user can act on."""
    if exc.returncode == analysis_run.EXIT_BACKEND_UNAVAILABLE:
        return (
            f"analysis stopped: the {backend!r} backend is not "
            f"installed here, so none of the {total} target(s) could have "
            "been analyzed. Install the analysis extra "
            "(music-dj-tools[analysis]) and run this again."
        )
    return (
        f"analysis stopped after a failure that is not about these files "
        f"({exc}); the remaining targets of {total} were not attempted "
        "because every chunk would meet the same wall. See the job log."
    )


#: Lines of subprocess output kept per job. A ring, so a library-scale run
#: cannot grow the status payload without bound.
LOG_RING: int = 400

#: The phases in which a job still owns the one slot. Everything it publishes
#: - counters, log ring, ``queue_signature`` - is still moving while its phase
#: is one of these, so a reader that needs a FINAL value has to wait for a
#: phase outside the set.
ACTIVE_PHASES: tuple[str, ...] = ("queued", "running")

#: The scope whose jobs publish a ``queue_signature``. Library and batch jobs
#: never target the unmapped backlog, so their silence about it means nothing;
#: an unmapped job's silence is a statement, and only the scope tells them
#: apart.
UNMAPPED_SCOPE: str = "unmapped"


class RefreshStatusOut(BaseModel):
    """The one-slot job's public, serializable state."""

    running: bool
    phase: str
    steps: list[str]
    current_step: str | None
    step_done: int
    step_total: int
    steps_completed: list[str]
    started_at: float | None
    finished_at: float | None
    error: str | None
    log_tail: list[str]
    recently_done_ids: list[str]


@dataclass
class _RefreshJob:
    started_at: float
    steps: list[str]
    batch_dir: Path | None = None
    #: "library" (whole sweep) | "batch" (staged files) | "unmapped" (no
    #: rekordbox mapping). Batch is implied by ``batch_dir``.
    scope: str = "library"
    #: Enabled steps this scope cannot run, dropped BEFORE the 202 so the
    #: reported ``steps`` are what will run. Reason logged by the worker.
    skipped_steps: list[str] = field(default_factory=list)
    phase: str = "queued"            # queued | running | done | error
    current_step: str | None = None
    step_done: int = 0
    step_total: int = 0
    steps_completed: list[str] = field(default_factory=list)
    error: str | None = None
    log: deque = field(default_factory=lambda: deque(maxlen=LOG_RING))
    recently_done_ids: deque = field(default_factory=lambda: deque(maxlen=200))
    queue_signature: str | None = None   # unmapped scope: what the worker read
    finished_at: float | None = None
    #: Explicit one-track requests keyed by the analysis kind the caller asked
    #: for. The worker still uses the one shared analysis CLI, never a UI-only
    #: imitation of an analyzer.
    analysis_orders: dict[str, str] = field(default_factory=dict)


_job_lock = threading.RLock()


class _JOBS:
    """One-slot registry for the running refresh job (no global statements)."""

    current: _RefreshJob | None = None

    #: The most recent unmapped drain, kept BESIDE the one slot.
    #:
    #: The slot is replaceable and says nothing about this backlog once a
    #: library or batch job has taken it. A drain that cleared its
    #: ``queue_signature`` - the CLI reported a target it never admitted, so
    #: the queue must be retried rather than booked - is still making a
    #: statement about the backlog after it has left the slot, and the
    #: reconcile loop has to hear it. Without this the newer clear is
    #: invisible, the loop books an older signature over it, and a target
    #: restored byte-identically sits behind ``unchanged`` for good.
    #:
    #: Assigned under ``_job_lock`` when the job is created, at the same
    #: moment as ``current``, so it can never lag behind a ``_wait`` on the
    #: status route. It therefore holds RUNNING drains too, and a reader that
    #: needs a final verdict has to check the phase - the same check it owes
    #: ``current``.
    last_unmapped: _RefreshJob | None = None


def _log(job: _RefreshJob, line: str) -> None:
    job.log.append(f"[{time.strftime('%H:%M:%S')}] {line}")


def _run_cli(job: _RefreshJob, argv: list[str]) -> None:
    """Run a pipeline CLI, streaming stdout into the job log. Raises on rc!=0.

    Raises :class:`CliFailed`, which carries the exit code: callers have to
    tell "this file could not be analyzed" from "this file was not there" and
    from "no backend could have analyzed anything here".
    """
    # Always name the module. ``argv[:3]`` is ``<python> -m <module>``, so a
    # tail-only slice silently drops it the moment the command grows a flag,
    # and the job log - the only record a user or a test has of what ran -
    # stops saying which CLI it was.
    _log(job, "$ " + " ".join([*argv[1:3], *argv[3:][-6:]]))
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
        raise CliFailed(f"{argv[2] if len(argv) > 2 else argv[0]} exited {rc}", rc)


__all__ = ["LOG_RING", "_JOBS", "_RefreshJob", "_job_lock", "_log", "_run_cli"]
