"""Did PR-head test selection let a failure through to the merge queue? (DEVOPS-20)

Read-only. This is the measurement that replaces the skipped shadow trial (the maintainer, Thu 1 Oct
2026, ADR-NEW-pr-heads-run-affected-tests): for a window, it finds every Trunk merge-queue
draft (`trunk-merge/*`) whose `pytest fast lane (shard N of 5)` job FAILED, reads the
failing test modules from that job's log, and asks of each one: did the last PR-head run of
any PR in that batch run this module?

    covered  a member PR's last PR-head run ran the module (full suite, or selected).
    escape   every member's record was read and none ran it: selection let it through.
    unknown  some member's record could not be read, and no readable record ran it.

A failed shard job whose log names no failing test (a wall-budget kill, a runner loss) is
listed as `unattributed`: no module can be judged, and it is not counted either way.

FAIL LOUD. Missing data is never zero escapes. Any record, log, PR body or run listing that
cannot be read makes the run exit 3 (UNKNOWN), and the escape count is then printed as a
lower bound. Exit 0 means every failing module in the window was judged; the escape count
is a metric, not a failure, so a measured escape still exits 0.

Records are kept three days (the repo's ceiling for a per-run byproduct), so measure a
window within three days of its end: a member head older than that reads UNKNOWN, loudly.

Draft RUN conclusions are not read: a draft run concludes `cancelled` even when Trunk
recorded the test as failed or passed (docs/research/trunk-mq-optimizations-2026-10-01.md,
section 01), so the shard JOB conclusion is the signal.

Usage:
    python -m scripts.ci_selection_escapes --since 2026-10-01T00:00:00Z --until 2026-10-08T00:00:00Z
    python -m scripts.ci_selection_escapes --since ... --until ... --json
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import zipfile
from dataclasses import asdict, dataclass, field
from typing import Protocol

from scripts.ci_failure_ids import failed_identities
from scripts.ci_test_selection import RECORD_NAME

DEFAULT_REPO = "maintainer/music-dj-tools"
DRAFT_PREFIX = "trunk-merge/"
ARTIFACT_PREFIX = "pr-test-selection-"
SHARD_JOB = re.compile(r"^pytest fast lane \(shard \d+ of \d+\)$")
#: The Actions runs search returns at most this many rows for one query.
RUNS_SEARCH_CAP = 1000
EXIT_UNKNOWN = 3
_MEMBER = re.compile(r"\[(\d+)\]\(")
_ATTEMPT = re.compile(r"-attempt-(\d+)$")


class Unknown(Exception):
    """An input the verdict depends on could not be read."""


class Api(Protocol):
    def json(self, path: str) -> object: ...
    def text(self, path: str) -> str: ...
    def binary(self, path: str) -> bytes: ...


# ---------------------------------------------------------------------------
# pure readers of real GitHub shapes
# ---------------------------------------------------------------------------


def queue_draft_runs(runs_page: dict) -> list[dict]:
    return [
        run
        for run in runs_page["workflow_runs"]
        if run["event"] == "pull_request" and run["head_branch"].startswith(DRAFT_PREFIX)
    ]


def failed_shard_jobs(jobs_page: dict) -> list[dict]:
    return [
        job
        for job in jobs_page["jobs"]
        if SHARD_JOB.match(job["name"]) and job["conclusion"] == "failure"
    ]


def failing_modules(log: str) -> set[str]:
    """Test modules named by FAILED/ERROR lines (scripts/ci_failure_ids.py reads them)."""
    modules: set[str] = set()
    for identity in failed_identities(log):
        kind, _, rest = identity.partition(" ")
        if kind in ("FAILED", "ERROR") and rest.startswith("tests/"):
            modules.add(rest.split("::", 1)[0])
    return modules


def batch_members(body: str | None) -> list[int]:
    """PR numbers a Trunk draft tests, from its body's "This pull request is testing" line."""
    for line in (body or "").splitlines():
        if line.startswith("This pull request is testing"):
            members = [int(n) for n in _MEMBER.findall(line.split(" - [", 1)[0])]
            if members:
                return members
    raise Unknown("the draft PR body names no PR under test")


def last_pr_head_run(runs_page: dict, before: str) -> dict | None:
    """The newest pull_request run created before `before` (ISO 8601 Z strings sort)."""
    runs = [
        run
        for run in runs_page["workflow_runs"]
        if run["event"] == "pull_request" and run["created_at"] < before
    ]
    return max(runs, key=lambda run: run["created_at"], default=None)


def selection_artifact(artifacts_page: dict, head_sha: str) -> dict | None:
    """The newest attempt's unexpired selection record for this head sha."""
    prefix = f"{ARTIFACT_PREFIX}{head_sha}-attempt-"
    candidates = [
        artifact
        for artifact in artifacts_page["artifacts"]
        if artifact["name"].startswith(prefix)
        and not artifact["expired"]
        and _ATTEMPT.search(artifact["name"])
    ]
    return max(candidates, key=lambda a: int(_ATTEMPT.search(a["name"]).group(1)), default=None)


def record_from_zip(blob: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return json.loads(archive.read(RECORD_NAME))


def member_status(record: dict | None, module: str) -> str:
    if record is None:
        return "unknown"
    if record["mode"] == "full":
        return "ran: full suite"
    if record["mode"] == "affected" and module in record["selected_modules"]:
        return "ran: selected"
    if record["mode"] == "affected":
        return "not selected"
    return f"unknown: record mode {record['mode']!r}"


def verdict(statuses: dict[int, str]) -> str:
    if any(status.startswith("ran:") for status in statuses.values()):
        return "covered"
    if statuses and all(status == "not selected" for status in statuses.values()):
        return "escape"
    return "unknown"


# ---------------------------------------------------------------------------
# assessment
# ---------------------------------------------------------------------------


@dataclass
class Failure:
    draft_run: int
    draft_branch: str
    job: str
    module: str
    verdict: str
    members: dict[int, str]


@dataclass
class Report:
    since: str
    until: str
    drafts: int = 0
    failed_shard_jobs: int = 0
    failures: list[Failure] = field(default_factory=list)
    unattributed: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)

    def count(self, kind: str) -> int:
        return sum(1 for failure in self.failures if failure.verdict == kind)


def _list_runs(api: Api, repo: str, since: str, until: str) -> list[dict]:
    runs: list[dict] = []
    page = 1
    while True:
        data = api.json(
            f"repos/{repo}/actions/workflows/ci.yml/runs?event=pull_request"
            f"&created={since}..{until}&per_page=100&page={page}"
        )
        if data["total_count"] > RUNS_SEARCH_CAP:
            raise Unknown(
                f"{data['total_count']} runs in {since}..{until} exceed the {RUNS_SEARCH_CAP}-row "
                "search cap; narrow the window"
            )
        runs += data["workflow_runs"]
        if not data["workflow_runs"] or len(runs) >= data["total_count"]:
            break
        page += 1
    if len(runs) < data["total_count"]:
        raise Unknown(f"listed {len(runs)} of {data['total_count']} runs")
    return runs


def _member_record(api: Api, repo: str, member: int, before: str) -> dict | None:
    """The member PR's last PR-head selection record, or None when it cannot be read."""
    pull = api.json(f"repos/{repo}/pulls/{member}")
    runs = api.json(
        f"repos/{repo}/actions/workflows/ci.yml/runs?event=pull_request"
        f"&branch={pull['head']['ref']}&created=%3C{before}&per_page=100"
    )
    run = last_pr_head_run(runs, before)
    if run is None:
        return None
    artifacts = api.json(f"repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100")
    artifact = selection_artifact(artifacts, run["head_sha"])
    if artifact is None:
        return None
    return record_from_zip(api.binary(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip"))


def _assess_draft(api: Api, repo: str, run: dict, report: Report) -> None:
    jobs = failed_shard_jobs(
        api.json(f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100")
    )
    if not jobs:
        return
    report.failed_shard_jobs += len(jobs)
    owner = repo.split("/", 1)[0]
    pulls = api.json(f"repos/{repo}/pulls?state=all&head={owner}:{run['head_branch']}")
    if not pulls:
        raise Unknown(f"no draft PR found for {run['head_branch']}")
    members = batch_members(pulls[0]["body"])
    records = {member: _member_record(api, repo, member, run["created_at"]) for member in members}
    for member, record in records.items():
        if record is None:
            report.unknowns.append(
                f"run {run['id']}: no readable selection record for PR #{member}"
            )
    for job in jobs:
        modules = failing_modules(api.text(f"repos/{repo}/actions/jobs/{job['id']}/logs"))
        if not modules:
            report.unattributed.append(f"run {run['id']} {job['name']}: no failing test named")
        for module in sorted(modules):
            statuses = {m: member_status(r, module) for m, r in records.items()}
            report.failures.append(
                Failure(
                    run["id"], run["head_branch"], job["name"], module, verdict(statuses), statuses
                )
            )


def assess(api: Api, repo: str, since: str, until: str) -> Report:
    report = Report(since=since, until=until)
    try:
        drafts = queue_draft_runs({"workflow_runs": _list_runs(api, repo, since, until)})
    except (Unknown, KeyError, subprocess.CalledProcessError) as error:
        report.unknowns.append(f"run listing: {error}")
        return report
    report.drafts = len(drafts)
    for run in drafts:
        try:
            _assess_draft(api, repo, run, report)
        except (
            Unknown,
            KeyError,
            ValueError,
            zipfile.BadZipFile,
            subprocess.CalledProcessError,
        ) as error:
            report.unknowns.append(f"run {run['id']} ({run['head_branch']}): {error!r}")
    return report


# ---------------------------------------------------------------------------
# GitHub through gh, and the CLI
# ---------------------------------------------------------------------------


class GhApi:
    """`gh api`, nothing else. Logs need --allow-escape-sequences or gh refuses them."""

    def _run(self, path: str, *flags: str) -> bytes:
        return subprocess.run(["gh", "api", *flags, path], capture_output=True, check=True).stdout

    def json(self, path: str) -> object:
        return json.loads(self._run(path))

    def text(self, path: str) -> str:
        return self._run(path, "--allow-escape-sequences").decode("utf-8", errors="replace")

    def binary(self, path: str) -> bytes:
        return self._run(path)


def render(report: Report) -> str:
    unknown = bool(report.unknowns) or report.count("unknown") > 0
    bound = " (LOWER BOUND: some inputs were UNKNOWN)" if unknown else ""
    lines = [
        f"selection escapes {report.since}..{report.until}",
        f"  queue drafts: {report.drafts}; failed shard jobs: {report.failed_shard_jobs}",
        f"  failing modules judged: {len(report.failures)}",
        f"  escapes: {report.count('escape')}{bound}",
        f"  covered: {report.count('covered')}; unknown: {report.count('unknown')}; "
        f"unattributed jobs: {len(report.unattributed)}",
    ]
    lines += [
        f"  [{f.verdict}] run {f.draft_run} {f.job}: {f.module} {f.members}"
        for f in report.failures
    ]
    lines += [f"  [unattributed] {line}" for line in report.unattributed]
    lines += [f"  [UNKNOWN] {line}" for line in report.unknowns]
    return "\n".join(lines)


def exit_code(report: Report) -> int:
    return EXIT_UNKNOWN if report.unknowns or report.count("unknown") else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--since", required=True, help="ISO 8601 UTC, e.g. 2026-10-01T00:00:00Z")
    parser.add_argument("--until", required=True, help="ISO 8601 UTC")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    arguments = parser.parse_args(argv)
    report = assess(GhApi(), arguments.repo, arguments.since, arguments.until)
    print(json.dumps(asdict(report), indent=2) if arguments.json else render(report))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
