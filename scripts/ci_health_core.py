"""Shared primitives for the CI health watchdog: verdicts, exit codes, and gh plumbing.

Extracted from scripts/ci_health_check.py so that scripts/ci_health_metrics.py can reuse
them without importing the checker back, which would be a cycle. The dependency runs one
way only:

    ci_health_core  <-  ci_health_metrics  <-  ci_health_check

Nothing here talks to the iteration-metrics store or decides a verdict; it is the vocabulary
the checks are written in plus the subprocess layer they read GitHub through, including how
one page of the actions/runs listing is validated and turned into Run objects. Both the
checker (page 1, newest-first) and the metrics catch-up (paged, filtered by `created`) read
that listing, so the parse lives here rather than in either caller.

-Claude
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

# ----- configuration ---------------------------------------------------------------

REPO = "private_owner/music-dj-tools"

GH_BIN = "gh"
GH_TIMEOUT_SECONDS = 60

EXIT_OK = 0
EXIT_BILLING = 2
EXIT_TRIGGER_DRIFT = 3
EXIT_FAILURE_RATE = 4
EXIT_STALENESS = 5
EXIT_ITERATION_SPEED = 6
EXIT_TRUNK_UNVERIFIED = 7
EXIT_PRECONDITION = 10

STALENESS_REMEDIATION = (
    "Pushes are landing but nothing runs. Check Actions is enabled for the repo "
    f"-> https://github.com/{REPO}/settings/actions"
)

# GitHub synthesizes 'dynamic' runs for Dependency Graph and Copilot submissions. They do
# not consume Actions runners and they succeed even while every real workflow is refused,
# so leaving them in would mask the exact billing signature R1 exists to catch.
EXCLUDED_RUN_EVENTS = ("dynamic",)

# GitHub's per_page ceiling for actions/runs. One page is one gh API call, whichever caller
# asks for it.
RUN_FETCH_COUNT = 100

# ----- data ------------------------------------------------------------------------


@dataclass
class Run:
    """One completed workflow run, reduced to the fields the checks reason about."""

    run_id: int
    name: str
    event: str
    head_branch: str
    head_sha: str
    conclusion: str
    started_at: datetime
    updated_at: datetime

    @property
    def duration_seconds(self) -> float:
        return (self.updated_at - self.started_at).total_seconds()

    @property
    def is_failure(self) -> bool:
        return self.conclusion == "failure"


@dataclass
class CheckResult:
    """Verdict for one named check, carrying its own denominator."""

    check: str
    ok: bool
    classification: str
    detail: str
    sample_size: int
    exit_code: int
    remediation: str = ""

    def verdict_line(self) -> str:
        token = "[OK]" if self.ok else "[ERROR]"
        return f"{token} {self.check}: {self.detail}"


class PreconditionError(RuntimeError):
    """Raised when the checker cannot trust its own inputs. Never swallowed."""


# ----- gh plumbing -----------------------------------------------------------------


def _run_gh(args: list[str]) -> str:
    """Call gh and return stdout, failing loudly on any non-zero exit."""
    if shutil.which(GH_BIN) is None:
        raise PreconditionError("gh CLI not found on PATH. Install it and run 'gh auth login'.")
    # check=False on purpose: the returncode is inspected below so the raised error can
    # name the exact gh invocation and carry its stderr, which CalledProcessError does not.
    completed = subprocess.run(
        [GH_BIN, *args],
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        invocation = " ".join(args)
        raise PreconditionError(
            f"gh {invocation} failed with exit {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout


def _gh_api_json(path: str) -> object:
    """GET a GitHub API path and parse the JSON body, failing loudly on garbage."""
    raw = _run_gh(["api", path])
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PreconditionError(f"gh api {path} returned unparseable JSON: {exc}") from exc


def _parse_github_timestamp(value: str, field: str, run_id: object) -> datetime:
    """Parse a GitHub ISO-8601 Z timestamp, naming the field when it is malformed."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise PreconditionError(f"run {run_id} has an unparseable {field}: {value!r}") from exc


# ----- actions/runs listing --------------------------------------------------------


def _completed_runs_page(payload: object) -> dict:
    """Validate one actions/runs page body and hand it back as a mapping.

    Separate from _parse_completed_runs because a paging caller also needs the RAW
    workflow_runs length and the listing's total_count off the same body, and re-deriving
    either from the filtered Run list would be a different number.
    """
    if not isinstance(payload, dict) or "workflow_runs" not in payload:
        raise PreconditionError("actions/runs response has no workflow_runs key")
    return payload


def _parse_completed_runs(payload: object) -> list[Run]:
    """One actions/runs page as Run objects, in whatever order GitHub returned them.

    Deliberately does NOT sort: the checker wants newest-first by started_at, while the
    metrics catch-up cares only about the set it has to fetch jobs for. Sorting here would
    impose one caller's ordering on the other for no reason.
    """
    runs: list[Run] = []
    for item in _completed_runs_page(payload)["workflow_runs"]:
        event = str(item["event"])
        if event in EXCLUDED_RUN_EVENTS:
            continue
        run_id = item["id"]
        runs.append(
            Run(
                run_id=int(run_id),
                name=str(item["name"]),
                event=event,
                head_branch=str(item["head_branch"]),
                head_sha=str(item["head_sha"]),
                conclusion=str(item["conclusion"]),
                started_at=_parse_github_timestamp(
                    item["run_started_at"], "run_started_at", run_id
                ),
                updated_at=_parse_github_timestamp(item["updated_at"], "updated_at", run_id),
            )
        )
    return runs


# ----- actions/runs/{id}/jobs ------------------------------------------------------

RunJobsPayloadCache = dict[int, object]


def _validate_run_jobs_payload(payload: object, run_id: int) -> list[dict[str, object]]:
    """Validate the fields shared by every job-verdict consumer.

    Job IDs are deliberately outside this minimal contract: ``classify_run`` consumes
    locked real API projections containing only name/status/conclusion. Consumers that
    materialize an ID-bearing object must validate that additional field themselves.
    """
    if not isinstance(payload, dict) or "jobs" not in payload:
        raise PreconditionError(f"actions/runs/{run_id}/jobs response has no jobs key")
    jobs = payload["jobs"]
    if not isinstance(jobs, list):
        raise PreconditionError(f"actions/runs/{run_id}/jobs response jobs was not a list")

    if "total_count" not in payload:
        raise PreconditionError(f"actions/runs/{run_id}/jobs response has no total_count key")
    total_count = payload["total_count"]
    if type(total_count) is not int or total_count < 0:
        raise PreconditionError(
            f"actions/runs/{run_id}/jobs response has invalid total_count: {total_count!r}"
        )
    if total_count > len(jobs):
        raise PreconditionError(
            f"actions/runs/{run_id}/jobs returned {len(jobs)} of {total_count} jobs; "
            "a partial page cannot support a truthful run verdict"
        )
    if total_count < len(jobs):
        raise PreconditionError(
            f"actions/runs/{run_id}/jobs response has invalid total_count "
            f"{total_count} for {len(jobs)} jobs"
        )

    required = ("name", "status", "conclusion")
    validated: list[dict[str, object]] = []
    for index, item in enumerate(jobs, start=1):
        if not isinstance(item, dict):
            raise PreconditionError(
                f"actions/runs/{run_id}/jobs response job {index} was not an object"
            )
        missing = [field for field in required if field not in item]
        if missing:
            raise PreconditionError(
                f"actions/runs/{run_id}/jobs response job {index} is missing: {missing}"
            )
        if not isinstance(item["name"], str) or not isinstance(item["status"], str):
            raise PreconditionError(
                f"actions/runs/{run_id}/jobs response job {index} has invalid name or status"
            )
        if item["conclusion"] is not None and not isinstance(item["conclusion"], str):
            raise PreconditionError(
                f"actions/runs/{run_id}/jobs response job {index} has invalid conclusion"
            )
        validated.append(item)
    return validated


def _cached_run_jobs_payload(
    run_id: int,
    cache: RunJobsPayloadCache,
    fetch_json: Callable[[str], object],
) -> dict:
    """One complete jobs page, fetched at most once for this cache.

    Metrics, failure-rate, and trunk verification all need the same endpoint but reduce it
    differently. Sharing the RAW validated body lets each consumer keep truthful semantics
    (notably job status for ``unknown``) without paying for the same GitHub call twice.
    """
    payload = cache.get(run_id)
    if payload is None:
        payload = fetch_json(f"repos/{REPO}/actions/runs/{run_id}/jobs?per_page=100")
    _validate_run_jobs_payload(payload, run_id)
    cache[run_id] = payload
    return payload

# A job that never executed is not a job that passed, and the two are indistinguishable
# downstream unless something says so out loud. `cancelled` is the one that misleads: it
# reads as benign, nothing alerts on it, and `gh run watch --exit-status` even returns 1
# for it, so the same state scans as fine from one angle and as a hard failure from
# another. `null` is a job the run never got to at all.
NON_EXECUTING_CONCLUSIONS = ("cancelled", "null", "None", "")


@dataclass
class Job:
    """One job of a workflow run, reduced to what a trunk-coverage verdict needs."""

    job_id: int
    name: str
    conclusion: str

    @property
    def passed(self) -> bool:
        # 'skipped' counts as passing: a path-filtered job legitimately has nothing to do,
        # which is different from a job that had work and did not run it.
        return self.conclusion in ("success", "skipped")

    @property
    def never_executed(self) -> bool:
        return self.conclusion in NON_EXECUTING_CONCLUSIONS

    @property
    def failed(self) -> bool:
        return self.conclusion == "failure"


def _validated_job_id(item: dict[str, object], run_id: int, index: int) -> int:
    """Return one GitHub job ID without coercing malformed API values."""
    if "id" not in item:
        raise PreconditionError(
            f"actions/runs/{run_id}/jobs response job {index} is missing: ['id']"
        )
    job_id = item["id"]
    if type(job_id) is not int or job_id <= 0:
        raise PreconditionError(
            f"actions/runs/{run_id}/jobs response job {index} has invalid id: {job_id!r}"
        )
    return job_id


def fetch_run_jobs(
    run_id: int, job_payloads: RunJobsPayloadCache | None = None
) -> list[Job]:
    """Every job of one run. Raises rather than returning an empty list on a bad body."""
    cache = {} if job_payloads is None else job_payloads
    payload = _cached_run_jobs_payload(run_id, cache, _gh_api_json)
    return [
        Job(
            job_id=_validated_job_id(item, run_id, index),
            name=str(item["name"]),
            conclusion=str(item.get("conclusion")),
        )
        for index, item in enumerate(payload["jobs"], start=1)
    ]
