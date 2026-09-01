#!/usr/bin/env python3
"""Verdict primitives for trunk CI health: per-job classification, survival, ancestry, fetch.

Split from scripts/trunk_job_verdict.py so the reasoning and the way it is rendered are
separately readable and testable, matching the existing ci_health_core split. The
dependency runs one way only:

    trunk_job_verdict_core  <-  trunk_job_verdict

Nothing here prints or files an issue; this is the vocabulary the CLI reports in. The
reasoning behind each rule lives on the function that implements it, and the user-facing
overview lives in scripts/trunk_job_verdict.py.

-Claude
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from scripts.ci_health_core import (
    REPO,
    PreconditionError,
    _gh_api_json,
    _parse_github_timestamp,
)

# ----- configuration ---------------------------------------------------------------

EXIT_OK = 0
EXIT_BURIED_FAILURE = 1
EXIT_PRECONDITION = 10

# A job that reached a red conclusion. 'timed_out' is a real failure wearing a different
# word: the work did not pass, and a run holding one is not healthy.
FAILING_JOB_CONCLUSIONS = ("failure", "timed_out")
# A job that reached a conclusion carrying no bad news. 'skipped' and 'neutral' jobs never
# executed, so they cannot hide a red; treating them as inconclusive would render almost
# every run 'unknown' and drain the word of meaning.
PASSING_JOB_CONCLUSIONS = ("success", "skipped", "neutral")

DEFAULT_WORKFLOW = "ci.yml"
DEFAULT_BRANCH = "main"
DEFAULT_LIMIT = 60

Verdict = Literal["pass", "failure", "unknown"]

VERDICT_PASS: Verdict = "pass"
VERDICT_FAILURE: Verdict = "failure"
VERDICT_UNKNOWN: Verdict = "unknown"

# ----- data ------------------------------------------------------------------------


@dataclass(frozen=True)
class JobOutcome:
    """One job inside a run, reduced to what the verdict reasons about."""

    name: str
    status: str
    conclusion: str | None

    @property
    def is_failing(self) -> bool:
        return self.conclusion in FAILING_JOB_CONCLUSIONS

    @property
    def is_conclusive(self) -> bool:
        """True only when this job reached an outcome that settles its own health.

        A job still queued or in progress has status != 'completed' and no trustworthy
        conclusion yet. A completed job whose conclusion is 'cancelled', 'action_required'
        or 'stale' reached no verdict about the work it was meant to do.
        """
        if self.status != "completed":
            return False
        return self.conclusion in FAILING_JOB_CONCLUSIONS + PASSING_JOB_CONCLUSIONS


@dataclass(frozen=True)
class RunOutcome:
    """One workflow run plus the jobs it actually reported."""

    run_id: int
    head_sha: str
    run_conclusion: str | None
    jobs: tuple[JobOutcome, ...]
    html_url: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class RunVerdict:
    """The per-job verdict for one run, carrying the evidence it was computed from."""

    run_id: int
    head_sha: str
    run_conclusion: str | None
    verdict: Verdict
    failing_jobs: tuple[str, ...]
    inconclusive_jobs: tuple[str, ...]
    job_count: int
    html_url: str = ""
    created_at: str = ""

    @property
    def is_buried(self) -> bool:
        """A real failure that the run's own top line does not report as one."""
        return self.verdict == VERDICT_FAILURE and self.run_conclusion != "failure"

    @property
    def summary(self) -> str:
        top = self.run_conclusion or "none"
        if self.verdict == VERDICT_FAILURE:
            jobs = ", ".join(self.failing_jobs)
            buried = " [BURIED]" if self.is_buried else ""
            return f"failure{buried}: run reads '{top}', failing jobs: {jobs}"
        if self.verdict == VERDICT_UNKNOWN:
            if self.job_count == 0:
                return f"unknown: run reads '{top}' and reported no jobs at all"
            jobs = ", ".join(self.inconclusive_jobs)
            return (
                f"unknown: run reads '{top}', "
                f"{len(self.inconclusive_jobs)}/{self.job_count} jobs never concluded: {jobs}"
            )
        return f"pass: all {self.job_count} jobs concluded without failure"


# ----- the verdict -------------------------------------------------------------------


def classify_run(run: RunOutcome) -> RunVerdict:
    """Decide a run's health from its JOBS. The run's own conclusion is never consulted.

    The top-line conclusion is carried through only so is_buried can report the DISAGREEMENT
    between what the run says about itself and what its jobs actually did. It has no vote in
    the verdict, which is the whole point of this module.
    """
    failing = tuple(job.name for job in run.jobs if job.is_failing)
    inconclusive = tuple(job.name for job in run.jobs if not job.is_conclusive)

    if failing:
        verdict = VERDICT_FAILURE
    elif inconclusive or not run.jobs:
        # No jobs at all is how GitHub renders a run cancelled while still queued. Nothing
        # was verified, so 'pass' would be a claim about work that never ran.
        verdict = VERDICT_UNKNOWN
    else:
        verdict = VERDICT_PASS

    return RunVerdict(
        run_id=run.run_id,
        head_sha=run.head_sha,
        run_conclusion=run.run_conclusion,
        verdict=verdict,
        failing_jobs=failing,
        inconclusive_jobs=inconclusive,
        job_count=len(run.jobs),
        html_url=run.html_url,
        created_at=run.created_at,
    )


# ----- survival ----------------------------------------------------------------------


@dataclass(frozen=True)
class SurvivalOutcome:
    """Whether one trunk run outlived a merge that landed while it was still in flight."""

    run_id: int
    head_sha: str
    run_conclusion: str | None
    overlapped_by: tuple[int, ...]
    survived: bool


def assess_survival(runs: list[RunOutcome]) -> list[SurvivalOutcome]:
    """Did each OVERLAPPED trunk run survive the next merge landing on top of it?

    THE DISCRIMINATING QUESTION, AND WHY THE OBVIOUS ONE IS WRONG
        "Did a post-fix trunk run conclude?" is passed by the failure state: 3 of 20
        pre-fix runs concluded too, simply because nothing merged fast enough to cancel
        them. A test the broken system passes measures nothing.

        The question that discriminates is whether a run SURVIVED a subsequent push
        created while it was still running. Only overlapped runs are counted here: a run
        nobody raced was never a test of the concurrency guard, and scoring it as a
        survivor would inflate the rate with runs that were never at risk.

    Needs no job data, only the run listing's own created_at and updated_at, so this is
    free relative to the per-job verdict above.
    """
    stamped = [run for run in runs if run.created_at and run.updated_at]
    outcomes: list[SurvivalOutcome] = []
    for run in stamped:
        started = _parse_github_timestamp(run.created_at, "created_at", run.run_id)
        finished = _parse_github_timestamp(run.updated_at, "updated_at", run.run_id)
        overlapped_by = tuple(
            other.run_id
            for other in stamped
            if other.run_id != run.run_id
            and started
            < _parse_github_timestamp(other.created_at, "created_at", other.run_id)
            <= finished
        )
        if not overlapped_by:
            continue
        outcomes.append(
            SurvivalOutcome(
                run_id=run.run_id,
                head_sha=run.head_sha,
                run_conclusion=run.run_conclusion,
                overlapped_by=overlapped_by,
                survived=run.run_conclusion != "cancelled",
            )
        )
    return outcomes


# ----- ancestry ----------------------------------------------------------------------


def is_built_on(candidate_sha: str, base_sha: str, repo_dir: Path | None = None) -> bool:
    """True when candidate_sha's tree CONTAINS base_sha, i.e. base_sha is its ancestor.

    Deliberately not a timestamp comparison. Under a fast-moving trunk with queued runs,
    time-ordering and ancestry-ordering routinely disagree, and a run that merely STARTED
    after a fix landed proves nothing about that fix. See the module docstring for the live
    case this rule was written from.

    A SHA git cannot resolve raises rather than returning False: a missing object means the
    clone is too shallow to answer the question, and answering it anyway would be a guess
    wearing the costume of a fact.
    """
    for sha in (base_sha, candidate_sha):
        resolved = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if resolved.returncode != 0:
            raise PreconditionError(
                f"commit {sha} is not present in this clone, so ancestry cannot be "
                "decided. Fetch it (git fetch origin) rather than falling back to a "
                "timestamp comparison."
            )

    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_sha, candidate_sha],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode not in (0, 1):
        raise PreconditionError(
            f"git merge-base --is-ancestor {base_sha} {candidate_sha} failed with exit "
            f"{completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.returncode == 0


# ----- fetching ----------------------------------------------------------------------


def _parse_jobs(payload: object, run_id: int) -> tuple[JobOutcome, ...]:
    if not isinstance(payload, dict) or "jobs" not in payload:
        raise PreconditionError(f"runs/{run_id}/jobs response has no jobs key")
    return tuple(
        JobOutcome(
            name=str(item["name"]),
            status=str(item["status"]),
            conclusion=None if item.get("conclusion") is None else str(item["conclusion"]),
        )
        for item in payload["jobs"]
    )


def fetch_run(run_id: int) -> RunOutcome:
    """One run and its jobs, straight from the API."""
    payload = _gh_api_json(f"repos/{REPO}/actions/runs/{run_id}")
    if not isinstance(payload, dict) or "head_sha" not in payload:
        raise PreconditionError(f"actions/runs/{run_id} response has no head_sha")
    jobs_payload = _gh_api_json(f"repos/{REPO}/actions/runs/{run_id}/jobs?per_page=100")
    conclusion = payload.get("conclusion")
    return RunOutcome(
        run_id=run_id,
        head_sha=str(payload["head_sha"]),
        run_conclusion=None if conclusion is None else str(conclusion),
        jobs=_parse_jobs(jobs_payload, run_id),
        html_url=str(payload.get("html_url", "")),
        created_at=str(payload.get("created_at", "")),
        updated_at=str(payload.get("updated_at", "")),
    )


def fetch_recent_runs(
    workflow: str, branch: str, limit: int, with_jobs: bool = True
) -> list[RunOutcome]:
    """The newest `limit` COMPLETED runs of one workflow on one branch, with their jobs.

    In-flight runs are excluded: a run still executing is legitimately unknown, and
    reporting it alongside settled history would put a moving number in an audit record.
    """
    listing = _gh_api_json(
        f"repos/{REPO}/actions/workflows/{workflow}/runs"
        f"?branch={branch}&status=completed&per_page={limit}"
    )
    if not isinstance(listing, dict) or "workflow_runs" not in listing:
        raise PreconditionError(f"workflows/{workflow}/runs response has no workflow_runs key")

    runs: list[RunOutcome] = []
    for item in listing["workflow_runs"][:limit]:
        run_id = int(item["id"])
        # Survival reasons only about the listing's own timestamps, so skip the per-run
        # job call: it is one API round trip per run and buys nothing for that question.
        jobs_payload: object = {"jobs": []}
        if with_jobs:
            jobs_payload = _gh_api_json(f"repos/{REPO}/actions/runs/{run_id}/jobs?per_page=100")
        conclusion = item.get("conclusion")
        runs.append(
            RunOutcome(
                run_id=run_id,
                head_sha=str(item["head_sha"]),
                run_conclusion=None if conclusion is None else str(conclusion),
                jobs=_parse_jobs(jobs_payload, run_id),
                html_url=str(item.get("html_url", "")),
                created_at=str(item.get("created_at", "")),
                updated_at=str(item.get("updated_at", "")),
            )
        )
    return runs


def load_runs_json(path: Path) -> list[RunOutcome]:
    """Offline input, so the detector can be exercised without touching the network."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreconditionError(f"cannot read runs fixture {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise PreconditionError(f"{path} must hold a JSON list of runs")
    return [
        RunOutcome(
            run_id=int(item["run_id"]),
            head_sha=str(item["head_sha"]),
            run_conclusion=item.get("run_conclusion"),
            jobs=tuple(
                JobOutcome(
                    name=str(job["name"]),
                    status=str(job.get("status", "completed")),
                    conclusion=job.get("conclusion"),
                )
                for job in item.get("jobs", [])
            ),
            html_url=str(item.get("html_url", "")),
            created_at=str(item.get("created_at", "")),
            updated_at=str(item.get("updated_at", "")),
        )
        for item in payload
    ]
