"""Scope decision and aggregated verdict for the merge-gating workflows (issue #4168).

A merge queue that waits for named check-runs (Trunk Merge Queue in draft-PR mode)
needs each gating workflow to report on EVERY pull request, docs-only ones included.
So `ci.yml` and `e2e.yml` trigger on every pull request and carry two small jobs:

- `scope`: `python3 -m scripts.ci_required_gate scope --workflow <file>` writes
  `in_scope=true|false` to `$GITHUB_OUTPUT`. A pull_request event is in scope when
  the merge commit under test changes a file the workflow's list in
  `scripts/ci_pr_scope.py` does not exclude; every other event is in scope, because
  its trigger-level filters (if any) already decided it should run. The heavy jobs
  run only when it says `true`.
- the aggregator (`ci gate`, `e2e verdict`): `python3 -m scripts.ci_required_gate
  verdict` reads `toJSON(needs)` and exits 0 iff the scope job succeeded and EITHER
  it said out of scope and every gated job was skipped, OR it said in scope and every
  gated job succeeded. Anything else (failure, cancelled, a skip while in scope, a job
  that ran while out of scope, an unreadable scope output) exits 1 naming each job and
  its result. A skipped check is never read as a pass here: whether a merge queue
  counts `skipped` as green is undocumented, so the aggregator always concludes
  success or failure.

Requirements:
- ✔︎ ✅ 🎯 A docs-only pull request is out of scope; `.planning/REQUIREMENTS.md` is in.
- ✔︎ ✅ 🎯 A non-pull_request event is always in scope.
- ✔︎ ✅ 🎯 An empty or unreadable diff is in scope or an error, never out of scope.
- ✔︎ ✅ 🎯 The verdict passes only on (out of scope, all skipped) or (in scope, all success).

Acceptance tests (tests/scripts/test_ci_required_gate.py):
- [if] every changed file is documentation [then] in_scope=false [⛔️ if a docs-only
  merge-queue batch occupies the pytest and e2e pools].
- [if] `.planning/REQUIREMENTS.md` changes alone [then] in_scope=true [⛔️ if the
  requirements ledger can drift past the PR gate again].
- [if] a gated job is skipped, failed or cancelled while in scope [then] the verdict
  fails naming it [⛔️ if a merge queue merges an unmeasured batch].

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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.ci_pr_scope import IN_RUN_PULL_REQUEST_SCOPES, workflow_would_run_for_files

SCOPE_JOB = "scope"
SCOPE_OUTPUT = "in_scope"
LISTED_FILES_CAP = 10


@dataclass(frozen=True)
class ScopeDecision:
    in_scope: bool
    reason: str


@dataclass(frozen=True)
class Verdict:
    ok: bool
    lines: tuple[str, ...]


# ----- scope ------------------------------------------------------------------


def decide_scope(workflow: str, event_name: str, changed_files: Sequence[str]) -> ScopeDecision:
    """Whether `workflow`'s heavy jobs must run for this event and change set."""
    if workflow not in IN_RUN_PULL_REQUEST_SCOPES:
        raise ValueError(
            f"{workflow} has no in-run scope in scripts/ci_pr_scope.py; "
            f"known: {sorted(IN_RUN_PULL_REQUEST_SCOPES)}"
        )
    if event_name != "pull_request":
        return ScopeDecision(True, f"event {event_name!r} is not path-scoped here")
    if not changed_files:
        return ScopeDecision(
            True, "the merge commit changes no files; an empty diff is unmeasured, so run"
        )
    paths, paths_ignore = IN_RUN_PULL_REQUEST_SCOPES[workflow]
    in_scope_files = [
        path
        for path in changed_files
        if workflow_would_run_for_files([path], paths=paths, paths_ignore=paths_ignore)
    ]
    if in_scope_files:
        listed = ", ".join(in_scope_files[:LISTED_FILES_CAP])
        more = len(in_scope_files) - LISTED_FILES_CAP
        suffix = f" and {more} more" if more > 0 else ""
        return ScopeDecision(True, f"{len(in_scope_files)} in-scope file(s): {listed}{suffix}")
    return ScopeDecision(
        False, f"all {len(changed_files)} changed file(s) are outside {workflow}'s scope"
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


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
    lines = [f"scope: result={scope_result} {SCOPE_OUTPUT}={in_scope}"]
    if scope_result != "success" or in_scope not in ("true", "false"):
        lines.append(f"FAIL scope: the scope decision did not complete (result {scope_result})")
        lines.extend(f"     {job}: {result}" for job, result in sorted(gated.items()))
        return Verdict(False, tuple(lines))
    expected = "success" if in_scope == "true" else "skipped"
    ok = True
    for job, result in sorted(gated.items()):
        if result == expected:
            lines.append(f"ok   {job}: {result}")
            continue
        ok = False
        why = "in scope, so it must succeed" if in_scope == "true" else "ran although out of scope"
        lines.append(f"FAIL {job}: {result} ({why})")
    return Verdict(ok, tuple(lines))


# ----- CLI --------------------------------------------------------------------


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"${name} is unset; this runs inside a GitHub Actions job")
    return value


def _run_scope(workflow: str) -> int:
    event_name = _required_env("GITHUB_EVENT_NAME")
    changed = merge_commit_changed_files(Path.cwd()) if event_name == "pull_request" else []
    decision = decide_scope(workflow, event_name, changed)
    value = "true" if decision.in_scope else "false"
    with Path(_required_env("GITHUB_OUTPUT")).open("a", encoding="utf-8") as output:
        output.write(f"{SCOPE_OUTPUT}={value}\n")
    print(f"[ci-scope] {workflow}: {SCOPE_OUTPUT}={value} -- {decision.reason}")
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
    scope = commands.add_parser("scope", help="write in_scope=true|false to $GITHUB_OUTPUT")
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
