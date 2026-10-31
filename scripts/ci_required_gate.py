"""Scope decision and aggregated verdict for the merge-gating workflows (issue #4168).

A merge queue that waits for named check-runs (Trunk Merge Queue in draft-PR mode)
needs each gating workflow to report on EVERY pull request, docs-only ones included.
So `ci.yml` and `e2e.yml` trigger on every pull request and carry two small jobs:

- `scope`: `python3 -m scripts.ci_required_gate scope --workflow <file>` writes
  `in_scope=true|false` to `$GITHUB_OUTPUT`. A pull_request event is in scope when
  the merge commit under test changes a file the workflow's list in
  `scripts/ci_pr_scope.py` does not exclude; every other event is in scope, because
  its trigger-level filters (if any) already decided it should run. The heavy jobs
  run only when it says `true`. For ci.yml it also writes `skip_jobs=<comma list>`
  (TEST-BUDGET round 4, Fri 30 Oct 2026): the fixed jobs in `CI_JOB_PATHS` whose
  result none of the changed files can affect. Each of those jobs carries an `if:`
  that reads its own name out of that list; an empty or absent list runs the job.
- the aggregator (`ci gate`, `e2e verdict`): `python3 -m scripts.ci_required_gate
  verdict` reads `toJSON(needs)` and exits 0 iff the scope job succeeded and EITHER
  it said out of scope and every gated job was skipped, OR it said in scope and every
  gated job succeeded, except a job `skip_jobs` names, which must be skipped. Anything
  else (failure, cancelled, a skip the scope job did not decide, a job that ran while
  out of scope or while listed in `skip_jobs`, an unreadable scope output) exits 1
  naming each job and its result. A skipped check is never read as a pass here:
  whether a merge queue counts `skipped` as green is undocumented, so the aggregator
  always concludes success or failure.

Requirements:
- ✔︎ ✅ 🎯 A docs-only pull request is out of scope; `.planning/REQUIREMENTS.md` is in.
- ✔︎ ✅ 🎯 A non-pull_request event is always in scope.
- ✔︎ ✅ 🎯 An empty or unreadable diff is in scope or an error, never out of scope.
- ✔︎ ✅ 🎯 The verdict passes only on (out of scope, all skipped) or (in scope, all success).
- ✔︎ ✅ 🎯 In scope, a job `skip_jobs` names must be skipped; one it does not name must succeed.

Acceptance tests ((private path)):
- [if] every changed file is documentation [then] in_scope=false [⛔️ if a docs-only
  merge-queue batch occupies the pytest and e2e pools].
- [if] `.planning/REQUIREMENTS.md` changes alone [then] in_scope=true [⛔️ if the
  requirements ledger can drift past the PR gate again].
- [if] a gated job is skipped, failed or cancelled while in scope [then] the verdict
  fails naming it [⛔️ if a merge queue merges an unmeasured batch].
- [if] a gated job is skipped while in scope and `skip_jobs` names it [then] the verdict
  passes it; [if] `skip_jobs` names it and it ran [then] the verdict fails it [⛔️ if a
  footprint skip reads as red, or a job runs past the decision unnoticed].

Standard library only (it imports nothing but `scripts.ci_pr_scope`), so the runner
image's own `python3` runs it with no venv and no install step on any host the light
pool can land on.

Supersedes: nothing ran on a docs-only pull request before; the trigger-level path
filters that skipped the whole workflow now live in `scripts.ci_pr_scope`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.ci_pr_scope import (
    CI_FIXED_JOBS,
    IN_RUN_PULL_REQUEST_SCOPES,
    ci_fixed_jobs_to_skip,
    e2e_gate_scripts,
    e2e_tier,
    workflow_would_run_for_files,
)

SCOPE_JOB = "scope"
SCOPE_OUTPUT = "in_scope"
TIER_OUTPUT = "e2e_tier"
E2E_WORKFLOW = Path(".github/workflows/e2e.yml")
#: ci.yml only: the fixed jobs the scope decision skips by footprint, comma-separated.
SKIP_OUTPUT = "skip_jobs"
#: The workflow whose scope job publishes `SKIP_OUTPUT`.
SKIP_WORKFLOW = "ci.yml"
LISTED_FILES_CAP = 10


@dataclass(frozen=True)
class ScopeDecision:
    in_scope: bool
    reason: str
    #: e2e.yml only, and only in scope: `smoke` or `full` (TEST-BUDGET round 2). None otherwise.
    e2e_tier: str | None = None


@dataclass(frozen=True)
class Verdict:
    ok: bool
    lines: tuple[str, ...]


# ----- scope ------------------------------------------------------------------


def decide_scope(
    workflow: str,
    event_name: str,
    changed_files: Sequence[str],
    gate_scripts: Collection[str],
) -> ScopeDecision:
    """Whether `workflow`'s heavy jobs must run for this event and change set.

    For e2e.yml the decision also carries the tier: `full` for every non-pull_request
    event and empty diff, else `e2e_tier` over the in-scope files, where `gate_scripts`
    are the `scripts/` files e2e.yml itself runs (`e2e_gate_scripts`), always full tier.
    """
    tiered = workflow == "e2e.yml"
    if tiered and not gate_scripts:
        raise ValueError("e2e.yml runs gate helpers under scripts/; an empty gate_scripts would classify them smoke")
    if workflow not in IN_RUN_PULL_REQUEST_SCOPES:
        raise ValueError(
            f"{workflow} has no in-run scope in scripts/ci_pr_scope.py; known: {sorted(IN_RUN_PULL_REQUEST_SCOPES)}"
        )
    if event_name != "pull_request":
        return ScopeDecision(True, f"event {event_name!r} is not path-scoped here", "full" if tiered else None)
    if not changed_files:
        return ScopeDecision(
            True,
            "the merge commit changes no files; an empty diff is unmeasured, so run",
            "full" if tiered else None,
        )
    paths, paths_ignore = IN_RUN_PULL_REQUEST_SCOPES[workflow]
    in_scope_files = [
        path for path in changed_files if workflow_would_run_for_files([path], paths=paths, paths_ignore=paths_ignore)
    ]
    if in_scope_files:
        listed = ", ".join(in_scope_files[:LISTED_FILES_CAP])
        more = len(in_scope_files) - LISTED_FILES_CAP
        suffix = f" and {more} more" if more > 0 else ""
        tier = e2e_tier(in_scope_files, gate_scripts) if tiered else None
        tier_note = f"; e2e tier {tier}" if tier else ""
        return ScopeDecision(True, f"{len(in_scope_files)} in-scope file(s): {listed}{suffix}{tier_note}", tier)
    return ScopeDecision(False, f"all {len(changed_files)} changed file(s) are outside {workflow}'s scope")


def decide_skipped_jobs(
    workflow: str, event_name: str, changed_files: Sequence[str], in_scope: bool
) -> tuple[str, ...]:
    """The ci.yml fixed jobs the scope job skips by footprint, in `CI_FIXED_JOBS` order.

    Empty for e2e.yml (its one gate has no per-job table), for every event but a
    pull_request (push and dispatch runs run every job), and for an out-of-scope
    change set (every job is already skipped by `in_scope`).
    """
    if workflow != SKIP_WORKFLOW or event_name != "pull_request" or not in_scope:
        return ()
    return tuple(ci_fixed_jobs_to_skip(changed_files))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def merge_commit_changed_files(repo: Path) -> list[str]:
    """Files the checked-out pull_request merge commit changes against its base parent.

    The pull_request checkout is GitHub's test merge commit (parents: base tip, PR
    head), so `HEAD^1..HEAD` is exactly the change set the heavy jobs would test.
    Renames are split into their delete and add so both paths are scoped.
    """
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
    if len(parents) != 3:
        raise RuntimeError(
            f"HEAD {parents[0] if parents else '?'} has {len(parents) - 1} parent(s), not the "
            "2 of a pull_request merge commit; check out with fetch-depth: 2"
        )
    diff = _git(repo, "diff", "--no-renames", "--name-only", "HEAD^1", "HEAD")
    return [line for line in diff.splitlines() if line]


# ----- verdict ----------------------------------------------------------------


def gate_verdict(needs: Mapping[str, Mapping[str, object]]) -> Verdict:
    """The aggregator's verdict over `toJSON(needs)`."""
    if SCOPE_JOB not in needs:
        raise ValueError(f"the aggregator must need the {SCOPE_JOB!r} job; got {sorted(needs)}")
    gated = {job: str(body.get("result")) for job, body in needs.items() if job != SCOPE_JOB}
    if not gated:
        raise ValueError("the aggregator needs no gated job, so it would measure nothing")
    scope = needs[SCOPE_JOB]
    scope_result = str(scope.get("result"))
    outputs = scope.get("outputs")
    in_scope = outputs.get(SCOPE_OUTPUT) if isinstance(outputs, Mapping) else None
    skipped_by_scope = _decided_skips(outputs)
    not_waivable = sorted(skipped_by_scope - set(CI_FIXED_JOBS))
    lines = [
        f"scope: result={scope_result} {SCOPE_OUTPUT}={in_scope} {SKIP_OUTPUT}={','.join(sorted(skipped_by_scope))}"
    ]
    if scope_result != "success" or in_scope not in ("true", "false"):
        lines.append(f"FAIL scope: the scope decision did not complete (result {scope_result})")
        lines.extend(f"     {job}: {result}" for job, result in sorted(gated.items()))
        return Verdict(False, tuple(lines))
    ok = True
    if not_waivable:
        ok = False
        lines.append(f"FAIL {SKIP_OUTPUT}: names {not_waivable}, which are not fixed jobs the scope decision may skip")
    for job, result in sorted(gated.items()):
        if in_scope == "false":
            expected, why = "skipped", "ran although out of scope"
        elif job in skipped_by_scope:
            expected, why = "skipped", "ran although the scope decision skipped it"
        else:
            expected, why = "success", "in scope, so it must succeed"
        if result == expected:
            note = " (by the scope decision)" if expected == "skipped" and in_scope == "true" else ""
            lines.append(f"ok   {job}: {result}{note}")
            continue
        ok = False
        lines.append(f"FAIL {job}: {result} ({why})")
    return Verdict(ok, tuple(lines))


def _decided_skips(outputs: object) -> frozenset[str]:
    """The job names in the scope job's `skip_jobs` output; empty when absent or blank."""
    if not isinstance(outputs, Mapping):
        return frozenset()
    raw = outputs.get(SKIP_OUTPUT)
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


# ----- CLI --------------------------------------------------------------------


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"${name} is unset; this runs inside a GitHub Actions job")
    return value


def _run_scope(workflow: str) -> int:
    event_name = _required_env("GITHUB_EVENT_NAME")
    changed = merge_commit_changed_files(Path.cwd()) if event_name == "pull_request" else []
    gate_scripts = e2e_gate_scripts(E2E_WORKFLOW.read_text(encoding="utf-8")) if workflow == "e2e.yml" else frozenset()
    decision = decide_scope(workflow, event_name, changed, gate_scripts)
    value = "true" if decision.in_scope else "false"
    skipped = decide_skipped_jobs(workflow, event_name, changed, decision.in_scope)
    with Path(_required_env("GITHUB_OUTPUT")).open("a", encoding="utf-8") as output:
        output.write(f"{SCOPE_OUTPUT}={value}\n")
        if decision.e2e_tier:
            output.write(f"{TIER_OUTPUT}={decision.e2e_tier}\n")
        if workflow == SKIP_WORKFLOW:
            output.write(f"{SKIP_OUTPUT}={','.join(skipped)}\n")
    print(f"[ci-scope] {workflow}: {SCOPE_OUTPUT}={value} -- {decision.reason}")
    if workflow == SKIP_WORKFLOW:
        print(f"[ci-scope] {workflow}: {SKIP_OUTPUT}={','.join(skipped) or '(none)'}")
    return 0


def _run_verdict() -> int:
    needs = json.loads(_required_env("NEEDS_JSON"))
    verdict = gate_verdict(needs)
    for line in verdict.lines:
        print(line)
    for line in verdict.lines:
        if line.startswith("FAIL"):
            print(f"::error::{line}")
    print("[ci-gate] verdict: " + ("SUCCESS" if verdict.ok else "FAILURE"))
    return 0 if verdict.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    scope = commands.add_parser(
        "scope", help="write in_scope=true|false (and, for ci.yml, skip_jobs=...) to $GITHUB_OUTPUT"
    )
    scope.add_argument("--workflow", required=True, choices=sorted(IN_RUN_PULL_REQUEST_SCOPES))
    commands.add_parser("verdict", help="aggregate $NEEDS_JSON into one exit code")
    args = parser.parse_args(argv)
    if args.command == "scope":
        return _run_scope(args.workflow)
    if args.command == "verdict":
        return _run_verdict()
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
