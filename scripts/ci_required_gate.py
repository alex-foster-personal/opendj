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
  that reads its own name out of that list; an empty or absent list runs the job. For
  ci.yml it also writes `carry=true|false` and `carry_prev=<P1 sha>` when the checked-out
  pull_request head is a lander reqs-only resolve whose previous head's `ci gate` was
  green; when `carry=true`, `skip_jobs` names every gated job so only `reqs-carry` runs
  the scoped checks.
- `reqs-carry-check`: runs `build_reqs_json --check`, `(private path)`,
  and `(private path)`; prints a `REQS-ONLY-CARRY` audit line last.
- the aggregator (`ci gate`, `e2e verdict`): `python3 -m scripts.ci_required_gate
  verdict` reads `toJSON(needs)` and exits 0 iff the scope job succeeded and EITHER
  it said out of scope and every gated job was skipped, OR it said in scope and every
  gated job succeeded, except a job `skip_jobs` names, which must be skipped, OR carry
  is true and every gated job was skipped while `reqs-carry` succeeded. Anything else
  (failure, cancelled, a skip the scope job did not decide, a job that ran while
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
- ✔︎ ✅ 🎯 A reqs-only lander resolve whose previous head's gate is green carries it; any failed
  condition runs the full CI (REQS-ONLY-CARRY, #10542).
- ✔︎ ✅ 🎯 A resolve overwrite of a non-ledger file main touched does not carry unless the blob
  matches main (#10568).

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

from scripts.ci_gate_inherited_red import Waiver, only_test_job_failed, waiver_from_environment
from scripts.ci_pr_scope import (
    CI_FIXED_JOBS,
    CI_JOBS_ONLY_ON_PULL_REQUEST,
    IN_RUN_PULL_REQUEST_SCOPES,
    E2E_OPT_IN_LABEL,
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
#: ci.yml only (CI-DYNSHARD): the JSON list of pytest shards the scope job launches; `[]` skips `test`.
SHARDS_OUTPUT = "shards"
#: The sharded pytest job the shard plan governs.
SHARD_JOB = "test"
#: The workflow whose scope job publishes `SKIP_OUTPUT`.
SKIP_WORKFLOW = "ci.yml"
LISTED_FILES_CAP = 10
#: ci.yml only (REQS-ONLY-CARRY, #10542): `true` when the head is a reqs-only lander resolve
#: whose previous head's `ci gate` was green, so that verdict carries; `false` otherwise.
CARRY_OUTPUT = "carry"
#: ci.yml only: the previous PR head (P1) whose `ci gate` carries, or '' when nothing carries.
CARRY_PREV_OUTPUT = "carry_prev"
#: The job that runs the scoped reqs checks instead of the full CI under carry.
CARRY_JOB = "reqs-carry"
#: The only paths a carried resolve may change beyond what main brings in.
CARRY_PATHS = frozenset({"reqs.json", ".planning/REQUIREMENTS.md"})
CARRY_TRAILER_KEY = "Lander-Resolve"
CARRY_TRAILER_VALUE = "reqs-only"
#: Every gated job; under carry the scope job names all of them in `skip_jobs`, in this order.
CARRY_SKIP_JOBS = (
    "test",
    "frontend-build",
    "frontend",
    "import-contracts",
    "audio-rust",
    "contracts",
    "quality",
    "fast",
)
#: The check run on P1 that must have concluded success.
CARRY_GATE_CHECK = "ci gate"
#: The scoped set `reqs-carry-check` runs (argv after the interpreter), in order.
REQS_CARRY_CHECKS: tuple[tuple[str, ...], ...] = (
    ("-m", "scripts.build_reqs_json", "--check"),
    ("-m", "pytest", "-q", "(private path)", "(private path)"),
)
CARRY_AUDIT = "REQS-ONLY-CARRY"
SHORT_SHA = 10


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


@dataclass(frozen=True)
class CarryDecision:
    carry: bool
    reason: str
    prev: str = ""
    head: str = ""


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
    workflow: str, event_name: str, changed_files: Sequence[str], in_scope: bool, *, e2e_opted_in: bool
) -> tuple[str, ...]:
    """The ci.yml fixed jobs the scope job skips by footprint, in `CI_FIXED_JOBS` order.

    Empty for e2e.yml (its one gate has no per-job table) and for an out-of-scope change
    set (every job is already skipped by `in_scope`). A push or a dispatch runs every job
    but `CI_JOBS_ONLY_ON_PULL_REQUEST`, whose tests `contracts` runs on those events.
    """
    if workflow != SKIP_WORKFLOW or not in_scope:
        return ()
    if event_name != "pull_request":
        return CI_JOBS_ONLY_ON_PULL_REQUEST
    return tuple(ci_fixed_jobs_to_skip(changed_files, e2e_opted_in=e2e_opted_in))


def pull_request_opted_into_e2e(event: Mapping[str, object]) -> bool:
    """Whether the pull_request event payload carries the `e2e` opt-in label."""
    pull_request = event.get("pull_request")
    labels = pull_request.get("labels", []) if isinstance(pull_request, Mapping) else []
    return any(isinstance(label, Mapping) and label.get("name") == E2E_OPT_IN_LABEL for label in labels)


def _event_payload() -> Mapping[str, object]:
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    return json.loads(Path(path).read_text(encoding="utf-8")) if path and Path(path).is_file() else {}


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


# ----- scope: the reqs-only carry ---------------------------------------------


def lander_resolve_commits(repo: Path) -> tuple[str, str, str]:
    """(R, P1, P2): the PR head the test merge carries, and R's two parents."""
    head_parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
    if len(head_parents) != 3:
        raise RuntimeError(
            f"HEAD {head_parents[0] if head_parents else '?'} has {len(head_parents) - 1} parent(s), not the "
            "2 of a pull_request merge commit; check out with fetch-depth: 2"
        )
    resolve = head_parents[2]
    resolve_parents = _git(repo, "rev-list", "--parents", "-n", "1", resolve).split()
    if len(resolve_parents) != 3:
        raise RuntimeError(
            f"the PR head {resolve[:SHORT_SHA]} has {len(resolve_parents) - 1} parent(s), "
            "not the 2 of a lander resolve merge commit"
        )
    return resolve, resolve_parents[1], resolve_parents[2]


def resolve_has_carry_trailer(repo: Path, resolve: str) -> bool:
    raw = _git(
        repo,
        "log",
        "-1",
        f"--format=%(trailers:key={CARRY_TRAILER_KEY},valueonly)",
        resolve,
    )
    return any(line.strip() == CARRY_TRAILER_VALUE for line in raw.splitlines() if line.strip())


def _tree_blob(repo: Path, rev: str, path: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{rev}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def resolve_extra_paths(repo: Path, prev: str, main: str, resolve: str) -> frozenset[str]:
    """Paths the resolve changes beyond main's versions and the allowed ledger edits.

    Filename subtraction alone is not enough: main may have changed a path and the resolve may
    still overwrite it with untested content. For non-ledger paths main touched, require the blob
    at the resolve to match main.
    """
    changed = {line for line in _git(repo, "diff", "--no-renames", "--name-only", prev, resolve).splitlines() if line}
    base = _git(repo, "merge-base", prev, main).strip()
    from_main = {line for line in _git(repo, "diff", "--no-renames", "--name-only", base, main).splitlines() if line}
    extra = changed - from_main
    for path in changed & from_main:
        if path in CARRY_PATHS:
            continue
        if _tree_blob(repo, resolve, path) != _tree_blob(repo, main, path):
            extra.add(path)
    return frozenset(extra)


def latest_gate_conclusion(payload: Mapping[str, object]) -> str | None:
    runs = payload.get("check_runs")
    if not isinstance(runs, list):
        return None
    matches = [entry for entry in runs if isinstance(entry, Mapping) and entry.get("name") == CARRY_GATE_CHECK]
    if not matches:
        return None

    def run_id(entry: Mapping[str, object]) -> int:
        raw = entry.get("id")
        return int(raw) if isinstance(raw, int) else -1

    newest = max(matches, key=run_id)
    conclusion = newest.get("conclusion")
    return str(conclusion) if conclusion is not None else None


def previous_gate_conclusion(sha: str, environ: Mapping[str, str]) -> str | None:
    repo = environ.get("GITHUB_REPOSITORY")
    if not repo:
        return None
    command = ["gh", "api", f"repos/{repo}/commits/{sha}/check-runs?check_name=ci%20gate&per_page=100"]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env=os.environ if environ is os.environ else {**os.environ, **dict(environ)},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return latest_gate_conclusion(payload)


def decide_carry(repo: Path, gate_conclusion: object) -> CarryDecision:
    try:
        resolve, prev, main = lander_resolve_commits(repo)
        if not resolve_has_carry_trailer(repo, resolve):
            return CarryDecision(False, "the PR head carries no 'Lander-Resolve: reqs-only' trailer")
        extra = resolve_extra_paths(repo, prev, main, resolve) - CARRY_PATHS
        if extra:
            listed = ", ".join(sorted(extra)[:LISTED_FILES_CAP])
            more = len(extra) - LISTED_FILES_CAP
            suffix = f" and {more} more" if more > 0 else ""
            return CarryDecision(
                False,
                f"the resolve changes PR-authored file(s) outside reqs.json/.planning/REQUIREMENTS.md: {listed}{suffix}",
            )
        if not callable(gate_conclusion):
            return CarryDecision(False, "the previous head gate read is unavailable")
        conclusion = gate_conclusion(prev)
        if conclusion != "success":
            return CarryDecision(
                False,
                f"the previous head {prev[:SHORT_SHA]}'s ci gate is {conclusion or 'unreadable'}, not success",
            )
        return CarryDecision(
            True,
            f"reqs-only lander resolve of {prev[:SHORT_SHA]}, whose ci gate is green",
            prev=prev,
            head=resolve,
        )
    except (subprocess.CalledProcessError, RuntimeError, OSError) as err:
        return CarryDecision(False, f"the PR head is not a two-parent merge: {err}")


def carry_audit_line(pr: str, prev: str, head: str, passed: bool) -> str:
    prev_short = (prev[:SHORT_SHA] if prev else "?") or "?"
    head_short = (head[:SHORT_SHA] if head else "?") or "?"
    result = "pass" if passed else "fail"
    return f"{CARRY_AUDIT} #{pr} prev={prev_short} head={head_short} result={result}"


def pull_request_identity(environ: Mapping[str, str]) -> tuple[str, str]:
    event_path = environ.get("GITHUB_EVENT_PATH", "").strip()
    if event_path:
        try:
            payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        pr = payload.get("pull_request") if isinstance(payload, Mapping) else None
        if isinstance(pr, Mapping):
            number = pr.get("number")
            head = pr.get("head") if isinstance(pr.get("head"), Mapping) else None
            sha = head.get("sha") if isinstance(head, Mapping) else None
            if number is not None:
                return str(number), str(sha) if sha else "?"
    ref = environ.get("GITHUB_REF", "")
    if ref.startswith("refs/pull/") and ref.endswith("/merge"):
        parts = ref.split("/")
        if len(parts) >= 3 and parts[2].isdigit():
            return parts[2], "?"
    return "?", "?"


def _run_check_command(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), check=False).returncode


def reqs_carry_check(run: object, pr: str, prev: str, head: str) -> tuple[int, str]:
    if not callable(run):
        raise TypeError("run must be callable")
    ok = True
    for args in REQS_CARRY_CHECKS:
        command = [sys.executable, *args]
        print(f"[reqs-carry] $ {' '.join(command)}")
        rc = int(run(command))
        if rc != 0:
            ok = False
            print(f"::error::[reqs-carry] {' '.join(command)} exited {rc}")
    return (0 if ok else 1, carry_audit_line(pr, prev, head, ok))


# ----- verdict ----------------------------------------------------------------


def gate_verdict(needs: Mapping[str, Mapping[str, object]], *, pr: str = "?", head: str = "?") -> Verdict:
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
    carry_raw = outputs.get(CARRY_OUTPUT) if isinstance(outputs, Mapping) else None
    carrying = carry_raw == "true"
    skipped_by_scope = _decided_skips(outputs)
    not_waivable = sorted(skipped_by_scope - _carry_allowed_skips(carrying))
    lines = [
        "scope: result="
        f"{scope_result} {SCOPE_OUTPUT}={in_scope} {CARRY_OUTPUT}={carry_raw} "
        f"{SKIP_OUTPUT}={','.join(sorted(skipped_by_scope))}"
    ]
    if scope_result != "success" or in_scope not in ("true", "false"):
        lines.append(f"FAIL scope: the scope decision did not complete (result {scope_result})")
        lines.extend(f"     {job}: {result}" for job, result in sorted(gated.items()))
        return Verdict(False, tuple(lines))
    ok = True
    if carrying and in_scope != "true":
        ok = False
        lines.append("FAIL scope: carry=true while out of scope")
    if carrying and CARRY_JOB not in gated:
        ok = False
        lines.append(f"FAIL {CARRY_JOB}: did not run (carry=true needs it in ci gate's needs)")
    if not_waivable:
        ok = False
        lines.append(f"FAIL {SKIP_OUTPUT}: names {not_waivable}, which are not fixed jobs the scope decision may skip")
    carry_prev = outputs.get(CARRY_PREV_OUTPUT, "") if isinstance(outputs, Mapping) else ""
    if not isinstance(carry_prev, str):
        carry_prev = ""
    for job, result in sorted(gated.items()):
        expected, why = _gated_job_expectation(
            job,
            carrying=carrying,
            in_scope=in_scope,
            skipped_by_scope=skipped_by_scope,
            outputs=outputs,
        )
        if result == expected:
            note = _gated_job_ok_note(job, expected, in_scope, outputs)
            lines.append(f"ok   {job}: {result}{note}")
            continue
        ok = False
        lines.append(f"FAIL {job}: {result} ({why})")
    if carrying:
        lines = [*lines, carry_audit_line(pr, carry_prev, head, ok)]
    return Verdict(ok, tuple(lines))


def apply_inherited_red(verdict: Verdict, waiver: Waiver) -> Verdict:
    """Pass a verdict whose only failure is `test` when main's red explains all of it
    (scripts/ci_gate_inherited_red.py); otherwise keep it failed, with the reason logged."""
    if not waiver.granted:
        return Verdict(False, (*verdict.lines, *waiver.lines))
    waived = tuple(
        line.replace("FAIL ", "ok   ", 1) + " [WAIVED: inherited main red]" if line.startswith("FAIL") else line
        for line in verdict.lines
    )
    return Verdict(True, (*waived, *waiver.lines))


def _planned_zero_shards(outputs: object) -> bool:
    """CI-DYNSHARD: the scope job planned an empty shard matrix, so `test` is skipped by design.

    Only the exact `[]` counts; a missing or unreadable plan keeps `test` required.
    """
    return isinstance(outputs, Mapping) and outputs.get(SHARDS_OUTPUT) == "[]"


def _decided_skips(outputs: object) -> frozenset[str]:
    """The job names in the scope job's `skip_jobs` output; empty when absent or blank."""
    if not isinstance(outputs, Mapping):
        return frozenset()
    raw = outputs.get(SKIP_OUTPUT)
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def _carry_allowed_skips(carrying: bool) -> set[str]:
    return set(CI_FIXED_JOBS) | (set(CARRY_SKIP_JOBS) if carrying else set())


def _gated_job_expectation(
    job: str,
    *,
    carrying: bool,
    in_scope: str | None,
    skipped_by_scope: frozenset[str],
    outputs: object,
) -> tuple[str, str]:
    if job == CARRY_JOB:
        if carrying:
            return "success", "the carried gate needs the scoped reqs checks green"
        return "skipped", "ran although the scope decision did not carry"
    if in_scope == "false":
        return "skipped", "ran although out of scope"
    if job in skipped_by_scope:
        return "skipped", "ran although the scope decision skipped it"
    if job == SHARD_JOB and _planned_zero_shards(outputs):
        return "skipped", "ran although zero shards were planned"
    return "success", "in scope, so it must succeed"


def _gated_job_ok_note(
    job: str,
    expected: str,
    in_scope: str | None,
    outputs: object,
) -> str:
    if expected == "skipped" and job == SHARD_JOB and _planned_zero_shards(outputs):
        return " (no test selected, zero shards planned)"
    if expected == "skipped" and in_scope == "true":
        return " (by the scope decision)"
    return ""


# ----- CLI --------------------------------------------------------------------


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"${name} is unset; this runs inside a GitHub Actions job")
    return value


#: e2e.yml's scope job resolves the event itself (ADR-NEW-e2e-after-ci-gate): a
#: workflow_run for a pull request whose `ci gate` is green is scoped as
#: `pull_request` (the checkout is that PR's merge ref); any other workflow_run
#: carries a skip reason and is out of scope. Both are optional; unset, the
#: GitHub event name decides as before.
EVENT_OVERRIDE_ENV = "MDT_CI_SCOPE_EVENT_NAME"
SKIP_REASON_ENV = "MDT_CI_SCOPE_SKIP"


def resolved_event_name(environ: Mapping[str, str]) -> str:
    """`MDT_CI_SCOPE_EVENT_NAME` when set, else `GITHUB_EVENT_NAME` (required)."""
    override = environ.get(EVENT_OVERRIDE_ENV, "").strip()
    if override:
        return override
    value = environ.get("GITHUB_EVENT_NAME")
    if not value:
        raise RuntimeError("$GITHUB_EVENT_NAME is unset; this runs inside a GitHub Actions job")
    return value


def _run_scope(workflow: str) -> int:
    event_name = resolved_event_name(os.environ)
    skip_reason = os.environ.get(SKIP_REASON_ENV, "").strip()
    gate_scripts = e2e_gate_scripts(E2E_WORKFLOW.read_text(encoding="utf-8")) if workflow == "e2e.yml" else frozenset()
    if skip_reason:
        if workflow != "e2e.yml":
            raise RuntimeError(f"${SKIP_REASON_ENV} is only read for e2e.yml, not {workflow}")
        changed: list[str] = []
        decision = ScopeDecision(False, f"skipped by the scope job: {skip_reason}")
    else:
        changed = merge_commit_changed_files(Path.cwd()) if event_name == "pull_request" else []
        decision = decide_scope(workflow, event_name, changed, gate_scripts)
    value = "true" if decision.in_scope else "false"
    e2e_opted_in = event_name == "pull_request" and pull_request_opted_into_e2e(_event_payload())
    skipped = decide_skipped_jobs(workflow, event_name, changed, decision.in_scope, e2e_opted_in=e2e_opted_in)
    carry = CarryDecision(False, "not a pull_request in ci.yml's scope")
    if workflow == SKIP_WORKFLOW and event_name == "pull_request" and decision.in_scope:
        carry = decide_carry(Path.cwd(), lambda sha: previous_gate_conclusion(sha, os.environ))
    if carry.carry:
        skipped = CARRY_SKIP_JOBS
    with Path(_required_env("GITHUB_OUTPUT")).open("a", encoding="utf-8") as output:
        output.write(f"{SCOPE_OUTPUT}={value}\n")
        if decision.e2e_tier:
            output.write(f"{TIER_OUTPUT}={decision.e2e_tier}\n")
        if workflow == SKIP_WORKFLOW:
            output.write(f"{SKIP_OUTPUT}={','.join(skipped)}\n")
            output.write(f"{CARRY_OUTPUT}={'true' if carry.carry else 'false'}\n")
            output.write(f"{CARRY_PREV_OUTPUT}={carry.prev if carry.carry else ''}\n")
    print(f"[ci-scope] {workflow}: {SCOPE_OUTPUT}={value} -- {decision.reason}")
    if workflow == SKIP_WORKFLOW:
        print(f"[ci-scope] {workflow}: {SKIP_OUTPUT}={','.join(skipped) or '(none)'}")
        print(f"[ci-scope] {workflow}: {CARRY_OUTPUT}={'true' if carry.carry else 'false'} -- {carry.reason}")
    return 0


def _run_reqs_carry_check() -> int:
    pr, event_head = pull_request_identity(os.environ)
    try:
        resolve, prev, _ = lander_resolve_commits(Path.cwd())
        head = resolve
    except (RuntimeError, subprocess.CalledProcessError):
        prev = "?"
        head = event_head
    code, audit = reqs_carry_check(_run_check_command, pr, prev, head)
    print(audit)
    return code


def _run_verdict() -> int:
    needs = json.loads(_required_env("NEEDS_JSON"))
    pr, head = pull_request_identity(os.environ)
    verdict = gate_verdict(needs, pr=pr, head=head)
    if not verdict.ok and only_test_job_failed(verdict.lines, SHARD_JOB):
        verdict = apply_inherited_red(verdict, waiver_from_environment())
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
    commands.add_parser(
        "reqs-carry-check",
        help="run the scoped reqs checks a carried reqs-only resolve needs",
    )
    args = parser.parse_args(argv)
    if args.command == "scope":
        return _run_scope(args.workflow)
    if args.command == "verdict":
        return _run_verdict()
    if args.command == "reqs-carry-check":
        return _run_reqs_carry_check()
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
