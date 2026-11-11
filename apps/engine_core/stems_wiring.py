"""Wire the stems job to the card's definition and to a measured ending.

Composition-root glue, the only place that may see all three packages: the
chassis (``jobs.runner``), the stems domain (``apps.stems.job``) and the web
layer that owns the coverage snapshot (``apps.webui.server.stems_scope``).

* ``app.state.stems_scope_fn`` -- set by ``apps.webui.server.app_wiring._bind_stem_and_usage``
  (the web layer that owns the coverage snapshot). This module only binds
  the outcome check onto the engine runner.
* the ``stems.separate`` outcome check -- succeeded only when every requested
  bundle validates in the roots the card reads (STEM-59). The check is bound
  to the engine's own runner. A second app in-process does not replace it.

-Claude
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from apps.engine_core.jobs.runner import JobRunner, Outcome, OutcomeCheck
from apps.stems import job as stems_job
from apps.webui.server.routes import ingest as ingest_routes

RootsFor = Callable[[dict[str, Any]], Sequence[Path]]


def stems_outcome_check(roots_for: RootsFor) -> OutcomeCheck:
    """The kind's check, measuring each job in the roots ``roots_for`` names."""

    def check(job: dict[str, Any], returncode: int, stderr: Sequence[str]) -> Outcome:
        verdict = stems_job.verify_outcome(job, returncode, stderr, roots=roots_for(job))
        return Outcome(
            status=verdict.status,
            message=verdict.message,
            error=verdict.error,
            detail=verdict.detail,
        )

    return check


def _roots_for_job(app: FastAPI, job: dict[str, Any]) -> tuple[Path, ...]:
    data_dir = job["payload"].get("data_dir")
    if isinstance(data_dir, str) and data_dir:
        # The worker writes through stem_storage for this data dir. Local mode
        # is that dir's state/stems, so another library is not credited.
        # Remote mode is the crate the card reads, not state/stems.
        return stems_job.stems_measure_roots(Path(data_dir))
    return ingest_routes._stem_roots(app)


def wire_stems_truth(app: FastAPI, runner: JobRunner) -> None:
    """Bind this app's stems measurement to ``runner`` and to no other runner.

    The roots the card validates against are read per job: a library-mode
    switch moves them, and a check pinned at boot would measure the old ones.
    The closure stays on this runner. Writing it into the process registry
    would let the next app's call retarget every runner that had not bound
    its own, including this one if it still read that registry.
    """
    runner.bind_outcome_check(
        stems_job.JOB_KIND,
        stems_outcome_check(lambda job: _roots_for_job(app, job)),
    )


__all__ = ["RootsFor", "stems_outcome_check", "wire_stems_truth"]
