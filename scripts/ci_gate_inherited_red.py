"""Inherited red: let `ci gate` pass a pull request whose only failing tests are main's.

While main is red and a trunk repair is in flight, a pull request that fails exactly the
tests main already fails is not at fault, and a red `ci gate` keeps the lander's
merge-during-red exemption from ever using it. This module relaxes ONE gated job, the
sharded pytest job `test`, and only when every condition holds:

  - every failing pytest identity on the pull request head is in main's failing set,
    measured at the pull request's merge base OR at main's newest completed push run;
  - at least one open pull request carries the `ci:trunk-repair` label;
  - the pull request's diff touches none of those failing tests' files;
  - no failed shard log contains additional non-test failure evidence.

Each pass prints a `::warning::` naming the inherited identities, so it is never silent.
Anything it cannot measure (no identities on a red shard, main's run or logs unreadable,
an identity that is not a pytest node id) fails closed: today's verdict, with a warning.

Identities are parsed by `scripts.ci_failure_ids.failed_identities`, the same parser the
fleet merge gate and the main-red baseline use. Standard library only: `ci gate` runs on
the light pool, whose runner images carry a bare `python3` and no `gh`.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.client import HTTPMessage
from pathlib import Path
from typing import IO

from scripts.ci_failure_ids import failed_identities, failure_beyond_tests

REPAIR_LABEL = "ci:trunk-repair"
TEST_JOB_PREFIX = "pytest fast lane"
CI_WORKFLOW = "ci.yml"
API = "https://api.github.com"
PYTEST_PREFIXES = ("FAILED ", "ERROR ")
HTTP_TIMEOUT_S = 30


class Unmeasured(RuntimeError):
    """Something the waiver needs could not be read, so it fails closed."""


@dataclass(frozen=True)
class Evidence:
    pr_ids: frozenset[str]
    main_ids: frozenset[str]
    repair_prs: tuple[int, ...]
    changed_files: frozenset[str]


@dataclass(frozen=True)
class Waiver:
    granted: bool
    lines: tuple[str, ...]


# ----- pure -----


def failing_test_file(identity: str) -> str | None:
    """`FAILED tests/a/b.py::t[x]` -> `tests/a/b.py`; None for anything not a pytest id."""
    for prefix in PYTEST_PREFIXES:
        if identity.startswith(prefix):
            node = identity[len(prefix) :]
            return node.split("::", 1)[0]
    return None


def decide_waiver(evidence: Evidence) -> Waiver:
    """Grant only when every condition in the module docstring holds."""
    if not evidence.pr_ids:
        return Waiver(False, ("::warning::inherited-red: the red shards named no failing test; not waived",))
    unmapped = sorted(i for i in evidence.pr_ids if failing_test_file(i) is None)
    if unmapped:
        return Waiver(False, (f"::warning::inherited-red: not pytest ids, not waived: {unmapped}",))
    new = sorted(evidence.pr_ids - evidence.main_ids)
    if new:
        return Waiver(False, (f"inherited-red: {len(new)} failure(s) not on main, not waived: {new}",))
    if not evidence.repair_prs:
        return Waiver(False, (f"inherited-red: no open {REPAIR_LABEL} pull request, not waived",))
    files = {failing_test_file(i) for i in evidence.pr_ids}
    touched = sorted(f for f in files if f in evidence.changed_files)
    if touched:
        return Waiver(False, (f"inherited-red: the diff touches failing test file(s) {touched}, not waived",))
    inherited = sorted(evidence.pr_ids)
    repairs = ", ".join(f"#{n}" for n in evidence.repair_prs)
    return Waiver(
        True,
        (
            f"::warning title=ci gate passed on INHERITED main red::{len(inherited)} failing test(s) are "
            f"main's, repair in flight ({repairs}): {'; '.join(inherited)}",
            *(f"inherited {i}" for i in inherited),
        ),
    )


def only_test_job_failed(lines: tuple[str, ...], test_job: str) -> bool:
    """The verdict failed on the `test` job's own failure and nothing else."""
    fails = [line for line in lines if line.startswith("FAIL")]
    return bool(fails) and all(line.startswith(f"FAIL {test_job}: failure ") for line in fails)


# ----- I/O -----


class _NoAuthOffHost(urllib.request.HTTPRedirectHandler):
    """Drop Authorization when a redirect leaves the requested host.

    GitHub answers the job-logs endpoint with a 302 to a pre-signed Azure Blob URL, and
    Azure refuses a request that also carries the GitHub token (401 "Server failed to
    authenticate the request"). urllib's default handler forwards every header.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _host(new.full_url) != _host(req.full_url):
            new.remove_header("Authorization")
        return new


def _host(url: str) -> str | None:
    return urllib.parse.urlsplit(url).hostname


def fetch_text(url: str, token: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.build_opener(_NoAuthOffHost).open(request, timeout=HTTP_TIMEOUT_S) as response:
        return response.read().decode("utf-8", errors="replace")


def _api(path: str, token: str, raw: bool = False) -> object:
    body = fetch_text(f"{API}/{path}", token)
    return body if raw else json.loads(body)


def _red_shard_identities(repo: str, run_id: str, attempt: str, token: str) -> frozenset[str]:
    jobs = _api(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100", token)["jobs"]
    ids: set[str] = set()
    for job in jobs:
        if not job["name"].startswith(TEST_JOB_PREFIX) or job.get("conclusion") != "failure":
            continue
        log = _api(f"repos/{repo}/actions/jobs/{job['id']}/logs", token, raw=True)
        if beyond_tests := failure_beyond_tests(log):
            raise Unmeasured(
                f"red shard {job['name']!r} (job {job['id']}) has unexplained failure: {'; '.join(beyond_tests)}"
            )
        found = failed_identities(log)
        if not found:
            raise Unmeasured(f"red shard {job['name']!r} (job {job['id']}) names no failing test")
        ids |= found
    return frozenset(ids)


def _main_ids_at(repo: str, run: Mapping[str, object], token: str) -> frozenset[str]:
    return _red_shard_identities(repo, str(run["id"]), str(run["run_attempt"]), token)


def main_failing_ids(repo: str, merge_base: str, token: str) -> frozenset[str]:
    """Main's failing pytest ids at the merge base and at main's newest completed push run."""
    runs: list[Mapping[str, object]] = []
    base_runs = _api(
        f"repos/{repo}/actions/workflows/{CI_WORKFLOW}/runs?head_sha={merge_base}&status=completed&per_page=1",
        token,
    )["workflow_runs"]
    runs.extend(base_runs[:1])
    newest = _api(
        f"repos/{repo}/actions/workflows/{CI_WORKFLOW}/runs?branch=main&event=push&status=completed&per_page=1",
        token,
    )["workflow_runs"]
    runs.extend(newest[:1])
    if not runs:
        raise Unmeasured(f"no completed {CI_WORKFLOW} run on main at {merge_base[:10]} or main's head")
    ids: set[str] = set()
    for run in runs:
        ids |= _main_ids_at(repo, run, token)
    return frozenset(ids)


def open_repair_prs(repo: str, token: str) -> tuple[int, ...]:
    issues = _api(f"repos/{repo}/issues?state=open&labels={REPAIR_LABEL}&per_page=100", token)
    return tuple(sorted(i["number"] for i in issues if "pull_request" in i))


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def collect_evidence(environ: Mapping[str, str]) -> Evidence:
    token = environ.get("GITHUB_TOKEN", "")
    if not token:
        raise Unmeasured("GITHUB_TOKEN is unset in the ci gate step")
    repo = environ["GITHUB_REPOSITORY"]
    event = json.loads(Path(environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    pull = event.get("pull_request")
    if not isinstance(pull, Mapping):
        raise Unmeasured("not a pull_request event")
    head, base = pull["head"]["sha"], pull["base"]["sha"]
    merge_base = _git("merge-base", base, head).strip()
    changed = frozenset(_git("diff", "--name-only", f"{merge_base}..{head}").split())
    pr_ids = _red_shard_identities(repo, environ["GITHUB_RUN_ID"], environ.get("GITHUB_RUN_ATTEMPT", "1"), token)
    return Evidence(pr_ids, main_failing_ids(repo, merge_base, token), open_repair_prs(repo, token), changed)


def try_waiver(collect: Callable[[], Evidence]) -> Waiver:
    """The waiver, or a fail-closed refusal naming why the evidence could not be read."""
    try:
        evidence = collect()
    except (Unmeasured, OSError, KeyError, ValueError, subprocess.CalledProcessError) as exc:
        return Waiver(False, (f"::warning::inherited-red: could not measure main's red ({exc}); failing closed",))
    return decide_waiver(evidence)


def waiver_from_environment() -> Waiver:
    return try_waiver(lambda: collect_evidence(os.environ))
