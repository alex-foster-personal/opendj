"""Which tests a pull request HEAD runs in the five pytest fast lane shards (DEVOPS-20).

the maintainer, Thu 1 Oct 2026, answered in chat: "Go now, skip the trial". A PR head runs only the
tests its change affects. Decision record: (private path).

A push to main selects too (the maintainer, Tue 27 Oct 2026: "NEVER blast CI by running every test...
on a PR, bundle or merge lane. Full-suite runs belong to scheduled nightly jobs and to a
stated hypothesis, never to merging"; (private path)).
It takes the same hard-capped selection over every file changed since the newest main commit
whose `ci gate` passed, plus the `CI-Tests:` lines of the PRs merged since then; the scope job
reads that range once (scripts/ci_main_push_range.py, CI_MAIN_PUSH_RANGE). When the range is
unknown it tests HEAD's own change (HEAD^1..HEAD). Repo variable `CI_MAIN_PUSH_TEST_SELECTION`
is its own revert: `full` keeps main pushes on the full suite; unset or `affected` selects.
A `workflow_dispatch` and a `schedule` run keep the FULL suite: ci.yml's 02:00Z schedule and
full-ci.yml's 03:11Z one are the nightly runs that catch what a selection missed.

A `ci:trunk-repair` PR is a PR head like any other (the maintainer, Tue 3 Nov 2026; reversal of the
Thu 1 Oct exemption, (private path)): it
takes the same hard-capped selection, its `CI-Tests:` lines (the tests failing on main) rank
at distance 0, and the push run after it lands selects them again from its body. The label keeps only
its routing to the main-fix runners in ci.yml (`CI_RUNS_ON_MAIN_FIX`).

Bundle merges (the maintainer, Tue 27 Oct 2026; (private path),
(private path)): a Trunk merge-queue draft tests a bundle of PRs, and it selects
over the bundle's whole diff, exactly as a PR head does, once the repo variable
`CI_QUEUE_TEST_SELECTION` also equals `affected`; unset or anything else keeps the queue on
the full suite. The full suite runs nightly (`full-ci.yml` schedule) instead. A PR body may
ADD test modules with `CI-Tests: <path> -- <reason>` lines; the selection is the union of
those and what the rules below select, never fewer. A queue draft has no author body, so
it takes the union of its PRs' declarations from CI_BUNDLE_DECLARATIONS, which the scope job
collects once (scripts/ci_bundle_declarations.py); an unreadable record is the full suite.

Mode, decided once per shard from the same inputs, so every shard agrees:

    full      the job runs today's full sharded lane, unchanged.
    affected  this shard runs the selected test modules assigned to it.
    empty     the selection, or this shard's slice of it, is explicitly empty: the shard
              reports success without starting pytest, and says why, loudly.

Selection happens ONLY when all of these hold, and the reason for the first one that does
not is logged and recorded:
  1. repo variable `CI_PR_TEST_SELECTION` equals `affected` (the kill switch; anything else,
     unset included, is the full suite);
  2. the event is `pull_request`, or a `push` to refs/heads/main while
     `CI_MAIN_PUSH_TEST_SELECTION` is unset or `affected`;
  3. the head ref does not start with `trunk-merge/` (a Trunk queue draft), unless repo
     variable `CI_QUEUE_TEST_SELECTION` also equals `affected`.

Without a hard cap, a PR head then falls back to FULL, never to an empty run, when a changed
path is global (`GLOBAL_PATTERNS`), when the import graph cannot resolve a changed Python path
(the UNKNOWN of `scripts.affected_tests`), when changed code is imported by a top-level
conftest, or when the selection cannot be computed for any reason at all. Under the hard cap
(`ci/test-budget.yml`, `cap: hard`) an unnarrowable change ranks the whole lane into the
budget instead (`capped_full_selection`), and any other failure refuses (HardCapError) rather
than run FULL. Every fallback names its cause in the log, the job summary and the record.

What is selected, and why each rule exists: scripts/ci_test_selection_rules.py.

Two TEST-CUT cuts ((private path)) are separate repo variables, read only when the
switch above is on, so each one reverts by deleting it without touching the other:
  - `CI_PR_SELECT_BY_NAME=ci-config` (item 3): workflows (`.github/*`), CI scripts
    (`scripts/ci_*`) and deleted modules are selected by name instead of forcing FULL.
    `ci/*` and `tests/fixtures/*` still force FULL until #7228.
  - `CI_PR_DEMOTE_WEAK_LINKS=<dir>[,<dir>...]` (item 2): under those test directories
    (`tests` is all of them), a module selected only by a weak link is not run at this
    head; it runs only where the full suite runs (a FULL merge-queue draft, the nightly
    full-ci.yml). The record lists it under `demoted`. While the merge queue is off (since
    Fri 30 Oct 2026) that is the nightly run alone, so leave this variable unset until a
    FULL draft runs again before every merge.
A value that is set but not understood is a FULL run that names the variable: a typo must
never quietly make a run smaller or quietly undo a cut.

Shards: the selected modules are spread over the shards by recorded duration
(`.test_durations`), greedy least-loaded first, so no shard does all the work and every
shard computes the same assignment.

Usage (the shard step in .github/workflows/ci.yml):
    python -m scripts.ci_test_selection select --shard 1 --shards 5 --out-dir DIR \\
        --ignore=tests/analysis ...
    python -m scripts.ci_test_selection junit-all-skipped REPORT.xml --modules DIR/shard-modules.txt

`select` prints exactly one line on stdout, the mode, and writes DIR/selection.json (the
record the escape measurement and the selection guard read, scripts/ci_selection_escapes.py
and scripts/ci_selection_guard.py) and, in affected mode, DIR/shard-modules.txt. Everything for humans goes to stderr and the job summary.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
import traceback
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

from scripts.affected_tests import REPO
from scripts.ci_test_budget import (
    DECLARED_DISTANCE,
    BudgetConfig,
    BudgetError,
    fit,
    load_config,
    summary_line,
)
from scripts.ci_test_selection_rules import (
    DURATIONS_NAME,
    FullSuite,
    Policy,
    Selection,
    capped_full_selection,
    lane_universe,
    select_tests,
)

SWITCH_VARIABLE = "CI_PR_TEST_SELECTION"
QUEUE_SWITCH_VARIABLE = "CI_QUEUE_TEST_SELECTION"
MAIN_SWITCH_VARIABLE = "CI_MAIN_PUSH_TEST_SELECTION"
MAIN_SWITCH_OFF = "full"
MAIN_RANGE_VARIABLE = "CI_MAIN_PUSH_RANGE"
MAIN_REF = "refs/heads/main"
BUNDLE_VARIABLE = "CI_BUNDLE_DECLARATIONS"
SWITCH_ON = "affected"
#: A Trunk queue draft is classified once, here, and the bundle collector imports it (Sol,
#: PR #7598): the head ref is `trunk-merge/pr-N/...` or `trunk-temp/pr-N/...`, the author is
#: the Trunk App, and the head lives in this repository. A branch name alone is
#: caller-controlled, so a human's `trunk-merge/x` branch is an ordinary PR head.
QUEUE_DRAFT_HEAD = re.compile(r"^trunk-(?:merge|temp)/pr-\d+/")
TRUNK_LOGIN = "trunk-io[bot]"
BY_NAME_VARIABLE = "CI_PR_SELECT_BY_NAME"
BY_NAME_ON = "ci-config"
DEMOTE_VARIABLE = "CI_PR_DEMOTE_WEAK_LINKS"
#: Schema 2 (Tue 27 Oct 2026) adds `queue_switch`, `declared_modules`, `universe_modules` and
#: `bundle_prs`. Schema 3 (Fri 30 Oct 2026, TEST-BUDGET) adds `budget`: the runner-minute
#: budget the run was fitted to, the planned minutes before and after, and the weak-link
#: modules it dropped (scripts/ci_test_budget.py); None when `select` ran without `--budget`.
#: Schema 4 adds `policy` and `demoted` (TEST-CUT items 2 and 3, (private path)).
#: Schema 5 (Tue 3 Nov 2026) adds `main_push_base`: the commit a main push diffed against.
SCHEMA = 5
RECORD_NAME = "selection.json"
SHARD_MODULES_NAME = "shard-modules.txt"


# ---------------------------------------------------------------------------
# event classification and the kill switch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    name: str
    head_ref: str
    head_sha: str
    labels: tuple[str, ...]
    pr_number: int | None
    #: The PR body at the triggering event; a queue draft's is Trunk's banner.
    body: str = ""
    #: True only for an authenticated Trunk queue draft (`is_queue_draft`).
    queue_draft: bool = False

    @property
    def main_push(self) -> bool:
        """A push to main: `head_ref` is the pushed ref and `head_sha` the pushed commit."""
        return self.name == "push" and self.head_ref == MAIN_REF

    @classmethod
    def from_payload(cls, name: str, payload: dict, repository: str) -> Event:
        """`repository` is GITHUB_REPOSITORY, which authenticates a queue draft's head.

        A push payload has no `pull_request`: its `ref` and `after` are the head.
        """
        pull_request = payload.get("pull_request") or {}
        head = pull_request.get("head") or {}
        pushed = name == "push" and not pull_request
        return cls(
            name=name,
            head_ref=str((payload.get("ref") if pushed else head.get("ref")) or ""),
            head_sha=str((payload.get("after") if pushed else head.get("sha")) or ""),
            labels=tuple(str(label["name"]) for label in pull_request.get("labels") or ()),
            pr_number=pull_request.get("number"),
            body=str(pull_request.get("body") or ""),
            queue_draft=is_queue_draft(payload, repository),
        )


def is_queue_draft(payload: dict, repository: str) -> bool:
    """True for a Trunk queue draft: its head ref, its author and its head repository agree."""
    pull_request = payload.get("pull_request") or {}
    head = pull_request.get("head") or {}
    return (
        bool(repository)
        and QUEUE_DRAFT_HEAD.match(str(head.get("ref") or "")) is not None
        and (pull_request.get("user") or {}).get("login") == TRUNK_LOGIN
        and (head.get("repo") or {}).get("full_name") == repository
    )


def full_suite_reason(
    switch: str | None, event: Event, queue_switch: str | None = None, main_switch: str | None = None
) -> str | None:
    """Why this run takes the full suite, or None when it is a PR head or a main push that selects.

    `queue_switch` is `CI_QUEUE_TEST_SELECTION`; None is the variable unset, which keeps a
    queue draft on the full suite. `main_switch` is `CI_MAIN_PUSH_TEST_SELECTION`: `full` keeps
    a main push on the full suite, and any value but unset, empty or `affected` does too, named.
    """
    if event.main_push:
        if (main_switch or "").strip() not in ("", SWITCH_ON):
            return (
                f"repo variable {MAIN_SWITCH_VARIABLE}={main_switch!r} keeps main pushes on the full "
                f"suite; it selects only when unset or {SWITCH_ON!r}"
            )
    elif event.name != "pull_request":
        return f"event `{event.name}` is not a pull request head or a push to main; only those select"
    if event.queue_draft and queue_switch != SWITCH_ON:
        return (
            f"head `{event.head_ref}` is a Trunk merge-queue draft; the queue runs everything "
            f"while repo variable {QUEUE_SWITCH_VARIABLE}={queue_switch!r} is not {SWITCH_ON!r}"
        )
    if switch != SWITCH_ON:
        return f"repo variable {SWITCH_VARIABLE}={switch!r}; selection runs only when it equals {SWITCH_ON!r}"
    return None


def policy_from(by_name: str | None, demote: str | None, root: Path) -> Policy:
    """The TEST-CUT policy the two repo variables ask for. Unset or empty is off.

    Raises FullSuite naming the variable on any value it does not understand.
    """
    by_name = (by_name or "").strip()
    if by_name not in ("", BY_NAME_ON):
        raise FullSuite(f"repo variable {BY_NAME_VARIABLE}={by_name!r} is neither unset nor {BY_NAME_ON!r}")
    directories = tuple(entry.strip().rstrip("/") for entry in (demote or "").split(",") if entry.strip())
    if (demote or "").strip() and not directories:
        raise FullSuite(f"repo variable {DEMOTE_VARIABLE}={demote!r} names no directory")
    for directory in directories:
        under_tests = directory == "tests" or directory.startswith("tests/")
        # `_under` compares strings, so `tests/./scripts` would match nothing yet read as set.
        canonical = all(part not in ("", ".", "..") for part in directory.split("/"))
        if not under_tests or not canonical or not (root / directory).is_dir():
            raise FullSuite(
                f"repo variable {DEMOTE_VARIABLE} names `{directory}`, which is not a directory under tests/"
            )
    return Policy(ci_config_by_name=by_name == BY_NAME_ON, demote_weak_under=directories)


# ---------------------------------------------------------------------------
# tests a PR body declares
# ---------------------------------------------------------------------------

#: `CI-Tests: tests/x/test_y.py -- why this PR needs it`, one module per line, at line start.
_DECLARATION_LINE = re.compile(r"^[ \t]*CI-Tests:(?P<rest>.*)$", re.M)
_DECLARATION = re.compile(r"^[ \t]*(?P<path>\S+)[ \t]+--[ \t]+\S")


def parse_declarations(body: str) -> tuple[list[str], list[str]]:
    """(declared module paths, errors) from a PR body's `CI-Tests:` lines.

    A line that is not `CI-Tests: <path> -- <reason>` is an error, never ignored and never
    half-read (Sol, PR #7598): the guard fails a record that carries one.
    """
    paths: list[str] = []
    errors: list[str] = []
    for match in _DECLARATION_LINE.finditer(body.replace("\r\n", "\n").replace("\r", "\n")):
        parsed = _DECLARATION.match(match["rest"])
        if parsed:
            paths.append(parsed["path"])
        else:
            errors.append(f"malformed `CI-Tests:{match['rest']}`, expected `CI-Tests: <path> -- <reason>`")
    return list(dict.fromkeys(paths)), errors


def declared_tests(body: str) -> list[str]:
    """The well-formed `CI-Tests:` module paths of a PR body, in order, deduplicated."""
    return parse_declarations(body)[0]


def undeclarable(paths: list[str], ignores: list[str], root: Path) -> list[str]:
    """Declared paths this lane never runs: not a test module, absent, or lane-ignored.

    The full lane carries the same ignores, so such a test would never run anywhere (Sol, PR
    #7598); it is an error the guard fails, whatever reason the body gives.
    """
    return [path for path in paths if not lane_universe([path], ignores, root)]


def bundle_record_problem(bundle: object) -> str | None:
    """Why a bundle record is malformed, or None; the selector and the guard share this check.

    An `unknown` record is well formed (it says why it could not read). A readable bundle needs
    unique positive PR numbers, `reasons` mapping some of those PRs to text, and string lists
    for `declared` and `errors` (Sol, PR #7598).
    """
    if not isinstance(bundle, dict) or not isinstance(bundle.get("bundle"), bool):
        return f"{bundle!r} is not a bundle record"
    if not bundle["bundle"] or bundle.get("unknown"):
        return None
    prs, reasons = bundle.get("prs"), bundle.get("reasons")
    if not isinstance(prs, list) or not prs or not all(type(pr) is int and pr > 0 for pr in prs):
        return f"prs is {prs!r}, not a non-empty list of PR numbers"
    if len(set(prs)) != len(prs):
        return f"prs {prs!r} repeats a PR"
    if not isinstance(reasons, dict) or not all(
        isinstance(pr, str) and pr.isdigit() and int(pr) in prs and isinstance(text, str) and text
        for pr, text in reasons.items()
    ):
        return f"reasons is {reasons!r}, not a mapping of the bundle's PR numbers to reason text"
    for field in ("declared", "errors"):
        values = bundle.get(field)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            return f"{field} is {values!r}, not a list of strings"
    return None


def bundle_declarations(bundle_json: str | None) -> tuple[list[str], list[int], list[str]] | None:
    """(declared modules, PRs, declaration errors) from a queue draft's bundle record.

    None when the record says this is not a bundle. An empty, unparseable or `unknown` record
    is FullSuite: the bundle cannot say what its PRs declared, and running more is the safe
    direction.
    """
    try:
        bundle = json.loads(bundle_json or "")
    except json.JSONDecodeError as error:
        raise FullSuite(
            f"{BUNDLE_VARIABLE} is not a bundle record ({error}); the bundle's declarations are unknown"
        ) from error
    problem = bundle_record_problem(bundle)
    if problem:
        raise FullSuite(f"the bundle record is malformed ({problem}); its declarations are unknown")
    if not bundle["bundle"]:
        return None
    if bundle.get("unknown"):
        raise FullSuite(f"the bundle's PR declarations could not be read: {bundle['unknown']}")
    return list(bundle["declared"]), list(bundle["prs"]), list(bundle["errors"])


def main_push_range(range_json: str | None) -> dict:
    """The scope job's CI_MAIN_PUSH_RANGE record (scripts/ci_main_push_range.py), `{}` when unreadable.

    Never FullSuite: a main push whose range is unknown tests HEAD's own change instead
    (`main_push_base`), because merged PR bodies are not something main can fix.
    """
    try:
        record = json.loads(range_json or "")
    except json.JSONDecodeError:
        return {}
    return record if isinstance(record, dict) and record.get("main_push") is True else {}


def main_push_base(root: Path, head_sha: str, record: dict) -> tuple[str, str]:
    """(base sha, why) for a main push: the range's green base, else HEAD's first parent.

    Raises FullSuite when the checkout is not the pushed commit: nothing it diffs is this push.
    """
    head = _git(root, "rev-parse", "HEAD")
    if head != head_sha:
        raise FullSuite(f"the checkout's HEAD is {head}, not the pushed commit {head_sha}")
    base = record.get("base")
    if isinstance(base, str) and base and record.get("head") == head_sha:
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base, head], cwd=root, capture_output=True, check=False
        )
        if ancestor.returncode == 0:
            return base, f"since {base[:10]}, the newest main commit with a green `ci gate`"
        problem = f"the range base {base[:10]} is not an ancestor of HEAD"
    else:
        problem = record.get("unknown") or f"{MAIN_RANGE_VARIABLE} is missing or names another head"
    return _git(root, "rev-parse", "HEAD^1"), f"HEAD's own change only ({problem})"


def range_changed_paths(root: Path, base: str) -> list[str]:
    """Paths changed between `base` and the checked-out HEAD; `--no-renames` lists a rename's old path too."""
    diff = _git(root, "diff", "--name-only", "--no-renames", base, "HEAD")
    return [line for line in diff.splitlines() if line]


def run_declarations(
    event: Event, bundle_json: str | None, range_record: dict | None = None
) -> tuple[list[str], list[int], list[str]]:
    """(declared modules, bundle PRs, declaration errors) for this run; FullSuite when unreadable.

    A main push's declarations are its merged PRs' (`range_record`), and their errors are
    returned empty: a merged body cannot be fixed, so `decide` reports and drops them instead.
    """
    if event.main_push:
        record = range_record or {}
        return list(record.get("declared") or []), list(record.get("prs") or []), []
    if not event.queue_draft:
        paths, errors = parse_declarations(event.body)
        return paths, [], errors
    bundle = bundle_declarations(bundle_json)
    if bundle is None:
        raise FullSuite("this is a Trunk queue draft, but the scope job's bundle record says it is not")
    return bundle


def add_declared(selection: Selection, declared: list[str]) -> None:
    """Add the declared modules, already validated by `undeclarable`, to the selection."""
    for path in declared:
        selection.add(path, "declared in the PR body (`CI-Tests:`)", DECLARED_DISTANCE)
    selection.modules = sorted(selection.reasons)


# ---------------------------------------------------------------------------
# shard assignment
# ---------------------------------------------------------------------------


def module_durations(ledger: dict[str, float]) -> dict[str, float]:
    """Recorded seconds per test module, summed over its node ids."""
    totals: dict[str, float] = {}
    for node, seconds in ledger.items():
        module = node.split("::", 1)[0]
        totals[module] = totals.get(module, 0.0) + float(seconds)
    return totals


#: CI-DYNSHARD: recorded test seconds one shard is planned to carry. Measured Wed 4 Nov 2026:
#: a FULL fifth is ~300 s of ledger time (1539 s union, run 34454681711), and a shard job's
#: fixed setup (checkout, venv, preflight) is 56-151 s (#8438). 240 s keeps an AFFECTED shard
#: under a FULL one while a typical later edit (a handful of modules) needs one runner.
SHARD_TARGET_SECONDS = 240.0


def _module_weights(modules: list[str], durations: dict[str, float]) -> dict[str, float]:
    """Recorded seconds per module; an untimed module weighs the median timed one."""
    known = [durations[m] for m in modules if m in durations]
    default = statistics.median(known) if known else 1.0
    return {m: durations.get(m, default) for m in modules}


def planned_shard_count(
    modules: list[str], durations: dict[str, float], max_shards: int, target_s: float = SHARD_TARGET_SECONDS
) -> int:
    """How many shards an AFFECTED selection needs: 0 for none, else ceil(seconds / target) in 1..max."""
    if not modules:
        return 0
    seconds = sum(_module_weights(modules, durations).values())
    return max(1, min(max_shards, math.ceil(seconds / target_s)))


def shard_matrix(record_path: Path, max_shards: int) -> list[int]:
    """The shard numbers to launch, read from the scope job's selection record.

    Fail-open: a missing, unreadable or FULL record, or one without a plan, launches every shard,
    so a crash can only cost runner minutes, never coverage.
    """
    every = list(range(1, max_shards + 1))
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return every
    count = record.get("shard_count") if isinstance(record, dict) else None
    if record.get("mode") != "affected" or not isinstance(count, int) or not 0 <= count <= max_shards:
        return every
    return list(range(1, count + 1))


def assign_shards(modules: list[str], durations: dict[str, float], shards: int) -> list[list[str]]:
    """Greedy least-loaded assignment, heaviest module first; ties go to the lower shard.

    A module the ledger has never timed (a new test file) weighs the median recorded module,
    so it is neither free nor dominant.
    """
    weights = _module_weights(modules, durations)
    loads = [0.0] * shards
    assignment: list[list[str]] = [[] for _ in range(shards)]
    for module in sorted(modules, key=lambda m: (-weights[m], m)):
        target = min(range(shards), key=lambda i: (loads[i], i))
        assignment[target].append(module)
        loads[target] += weights[module]
    return [sorted(group) for group in assignment]


# ---------------------------------------------------------------------------
# the PR diff
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def pr_changed_paths(root: Path, head_sha: str) -> list[str]:
    """Paths the PR changes against its merge base, read from the PR merge commit checkout.

    `actions/checkout` on a pull_request checks out the test merge commit, whose second
    parent is the PR head. That is verified, not assumed: a checkout of anything else
    cannot say what the PR changed. `--no-renames` lists a rename's old path too.
    """
    second_parent = _git(root, "rev-parse", "HEAD^2")
    if second_parent != head_sha:
        raise FullSuite(
            f"the checkout's HEAD^2 is {second_parent}, not the PR head {head_sha}, so the "
            "PR's own change set cannot be read from it"
        )
    merge_base = _git(root, "merge-base", "HEAD^1", "HEAD^2")
    diff = _git(root, "diff", "--name-only", "--no-renames", merge_base, "HEAD^2")
    return [line for line in diff.splitlines() if line]


# ---------------------------------------------------------------------------
# record, log and summary
# ---------------------------------------------------------------------------


@dataclass
class Record:
    schema: int
    mode: str
    reason: str
    switch: str | None
    queue_switch: str | None
    event: str
    head_ref: str
    head_sha: str
    pr_number: int | None
    changed_paths: list[str]
    selected_modules: list[str]
    declared_modules: list[str]
    #: The PRs a queue draft carries, whose declarations it used; empty for a PR head.
    bundle_prs: list[int]
    #: True for an authenticated Trunk queue draft.
    queue_draft: bool
    #: Why a `CI-Tests:` declaration is invalid; the guard always fails a record carrying one.
    declaration_error: str | None
    #: Test modules the lane collects; 0 when the run never computed it (a FULL record).
    universe_modules: int
    shards: dict[str, list[str]]
    reasons: dict[str, list[str]]
    generated_at: str
    #: TEST-BUDGET: the fit the selection was trimmed to (scripts.ci_test_budget.Fit.as_record),
    #: None when the run had no budget or took the FULL suite.
    budget: dict | None = None
    #: The TEST-CUT policy in force (`Policy` as a dict), None when it was not read.
    policy: dict | None = None
    #: Modules selected only by a weak link and not run at this head, with their rules.
    demoted: dict[str, list[str]] = dataclass_field(default_factory=dict)
    #: The commit a main push diffed against (`main_push_base`); None for every other run.
    main_push_base: str | None = None
    #: CI-DYNSHARD: shards this run's AFFECTED assignment spreads over (0 = no test, no shard);
    #: the scope job launches exactly that many. None on a FULL record (every shard runs).
    shard_count: int | None = None


class HardCapError(Exception):
    """A hard cap cannot be honored: the selector refuses to guess, and the run must fail, not widen.

    Without it the FULL fallback would run the uncapped lane (68 runner-minutes against a 10 cap)
    and hide the failure behind a green run.
    """


#: The `select` exit code for HardCapError; the shard step fails on it instead of running FULL.
HARD_CAP_REFUSED_RC = 3


def _full_or_refuse(hard: bool, problem: str, cause: BaseException | None = None) -> None:
    """Under a hard cap raise HardCapError; otherwise return, and the caller records FULL."""
    if hard:
        raise HardCapError(problem) from cause


def _precondition_problem(
    budget: BudgetConfig | None, shards: int, declarations_unknown: str | None, declaration_error: str | None
) -> str | None:
    """Why the selection cannot be computed honestly before it starts, or None."""
    if budget is not None and shards != budget.shards:
        return (
            f"the runtime shard count ({shards}) differs from the budget model's ({budget.shards}): "
            "the plan would be priced for a different number of shards than the run launches"
        )
    if declarations_unknown or declaration_error:
        return declarations_unknown or f"invalid declaration: {declaration_error}"
    return None


def _checked_declarations(
    event: Event, declared: list[str], errors: list[str], never_run: list[str], range_record: dict
) -> tuple[list[str], list[str], list[str]]:
    """(declared, errors, dropped) once the never-run paths are known.

    At a PR head a never-run path is an error the guard fails. A main push drops it, and its
    merged PRs' malformed lines, and says so in its reason: a merged body cannot be fixed.
    """
    if not event.main_push:
        never = [
            f"`CI-Tests:` path this lane never runs (typo, deleted, or lane-ignored): {path}" for path in never_run
        ]
        return declared, errors + never, []
    dropped = list(range_record.get("errors") or []) + [f"never runs: {path}" for path in never_run]
    return [path for path in declared if path not in never_run], errors, dropped


def _changed_paths(root: Path, event: Event, range_record: dict) -> tuple[list[str], str | None, str]:
    """(changed paths, main push base or None, why that base) for this run."""
    if event.main_push:
        base, since = main_push_base(root, event.head_sha, range_record)
        return range_changed_paths(root, base), base, since
    return pr_changed_paths(root, event.head_sha), None, ""


def _main_push_note(event: Event, since: str, prs: list[int], dropped: list[str]) -> str:
    """The reason suffix a main push adds: its base, its merged PRs, and what it dropped."""
    if not event.main_push:
        return ""
    note = f"; main push {since}, {len(prs)} merged PR(s)"
    if dropped:
        note += f"; {len(dropped)} merged `CI-Tests:` problem(s) ignored: " + "; ".join(dropped)
    return note


def _select_or_cap(
    changed: list[str],
    ignores: list[str],
    root: Path,
    budget: BudgetConfig | None,
    policy: Policy | None,
    main_push: bool = False,
) -> tuple[Selection, str | None]:
    """The narrowed selection, or under a hard cap the ranked whole lane with the reason it could not narrow.

    A hard cap has no full suite to fall back to (TEST-BUDGET round 5): the lane is ranked and
    cut instead, and the trunk checkpoint runs everything. Without a hard cap, or on an empty
    diff (indistinguishable from a failed one), the FullSuite propagates, and `decide` turns it into
    HardCapError under a hard cap. A main push's empty diff (a revert pair) is a real answer,
    not a failed read, so it ranks the lane like any unnarrowable change.
    """
    try:
        return select_tests(changed, ignores, root, policy), None
    except FullSuite as unnarrowable:
        if budget is None or not budget.hard or not (changed or main_push):
            raise
        return capped_full_selection(changed, ignores, root), str(unnarrowable)


def decide(  # noqa: PLR0913 -- the two TEST-CUT variables are keyword-only, one per cut
    switch: str | None,
    event: Event,
    root: Path,
    ignores: list[str],
    shards: int,
    queue_switch: str | None = None,
    bundle_json: str | None = None,
    budget: BudgetConfig | None = None,
    *,
    by_name: str | None = None,
    demote: str | None = None,
    main_switch: str | None = None,
    range_json: str | None = None,
    active_shards: int | None = None,
) -> Record:
    """The whole decision for this run. Any failure is a FULL record, except under a hard cap.

    Under a hard budget a failure is HardCapError instead: FULL is the uncapped lane, so it is
    not an answer the cap can give. The switches (`full_suite_reason`) still keep a run FULL.

    `bundle_json` is CI_BUNDLE_DECLARATIONS; it is read only for a `trunk-merge/` head.
    `active_shards` is the number of shards the run launched (the scope job's plan); None plans it
    here from the selection's recorded seconds (`planned_shard_count`), which is what the scope does.
    `budget` trims an affected selection to its runner-minute budget (ci/test-budget.yml); a main
    push is priced as a PR head. `range_json` is CI_MAIN_PUSH_RANGE, read only for a main push.
    """
    changed: list[str] = []
    policy: Policy | None = None
    universe: list[str] = []
    declared: list[str] = []
    bundle_prs: list[int] = []
    errors: list[str] = []
    declarations_unknown: str | None = None
    range_record = main_push_range(range_json) if event.main_push else {}
    base: str | None = None
    since = ""
    try:
        declared, bundle_prs, errors = run_declarations(event, bundle_json, range_record)
    except FullSuite as unreadable:
        declarations_unknown = str(unreadable)
    declared, errors, dropped_declarations = _checked_declarations(
        event, declared, errors, undeclarable(declared, ignores, root), range_record
    )
    #: Validated before every return, FULL ones included (Sol, PR #7598), so no path escapes the guard.
    declaration_error = "; ".join(errors) or None

    def record(
        mode: str,
        reason: str,
        selection: Selection | None = None,
        assignment: list[list[str]] | None = None,
        budget_block: dict | None = None,
    ) -> Record:
        return Record(
            schema=SCHEMA,
            mode=mode,
            reason=reason,
            switch=switch,
            queue_switch=queue_switch,
            event=event.name,
            head_ref=event.head_ref,
            head_sha=event.head_sha,
            pr_number=event.pr_number,
            changed_paths=changed,
            selected_modules=selection.modules if selection else [],
            declared_modules=declared,
            bundle_prs=bundle_prs,
            queue_draft=event.queue_draft,
            declaration_error=declaration_error,
            universe_modules=len(universe),
            shards={str(i + 1): group for i, group in enumerate(assignment or [])},
            reasons=selection.reasons if selection else {},
            generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            budget=budget_block,
            policy=asdict(policy) if policy else None,
            demoted=selection.demoted if selection else {},
            main_push_base=base,
            shard_count=None if mode == "full" else len(assignment or []),
        )

    reason = full_suite_reason(switch, event, queue_switch, main_switch)
    if reason:
        return record("full", reason)
    hard = budget is not None and budget.hard
    problem = _precondition_problem(budget, shards, declarations_unknown, declaration_error)
    if problem:
        _full_or_refuse(hard, f"cannot honor the hard cap: {problem}")
        return record("full", f"fell back to the full suite: {problem}")
    capped_over: str | None = None
    try:
        policy = policy_from(by_name, demote, root)
        changed, base, since = _changed_paths(root, event, range_record)
        selection, capped_over = _select_or_cap(changed, ignores, root, budget, policy, event.main_push)
        universe = lane_universe(selection.graph_paths, ignores, root)
        add_declared(selection, declared)
        ledger = json.loads((root / DURATIONS_NAME).read_text(encoding="utf-8"))
        durations = module_durations(ledger)
        budget_block: dict | None = None
        if budget is not None:
            fitted = fit(
                selection.modules, selection.reasons, durations,
                budget.for_context(event.queue_draft), budget, selection.distance,
            )  # fmt: skip
            selection.modules = fitted.kept
            budget_block = fitted.as_record()
        launched = (
            active_shards if active_shards is not None else planned_shard_count(selection.modules, durations, shards)
        )
        assignment = assign_shards(selection.modules, durations, launched)
    except FullSuite as fallback:
        _full_or_refuse(hard, f"cannot narrow or rank the change under the hard cap: {fallback}", fallback)
        return record("full", f"fell back to the full suite: {fallback}")
    except Exception as error:  # reported loudly below, and the fallback runs MORE tests
        traceback.print_exc(file=sys.stderr)
        _full_or_refuse(hard, f"selection could not be computed under the hard cap: {error!r}", error)
        return record("full", f"fell back to the full suite: selection could not be computed: {error!r}")
    demoted = f"; {len(selection.demoted)} weak-link module(s) not run at this head" if selection.demoted else ""
    demoted += _main_push_note(event, since, bundle_prs, dropped_declarations)
    if capped_over:
        return record(
            "affected",
            f"hard cap over a full-suite fallback ({capped_over}): {len(selection.modules)} of "
            f"{len(universe)} lane module(s) ranked into the budget{demoted}",
            selection,
            assignment,
            budget_block,
        )
    if not selection.modules and budget_block and budget_block["dropped"]:
        return record(
            "affected",
            f"hard cap dropped all {len(budget_block['dropped'])} affected module(s): none fits the budget{demoted}",
            selection,
            assignment,
            budget_block,
        )
    if not selection.modules:
        return record(
            "affected", f"no test module is affected by this change{demoted}", selection, assignment, budget_block
        )
    return record(
        "affected", f"{len(selection.modules)} affected test module(s){demoted}", selection, assignment, budget_block
    )


def _fit_of(record: Record):
    from scripts.ci_test_budget import Fit

    return Fit(**(record.budget or {}))


def shard_mode(record: Record, shard: int) -> str:
    if record.mode == "full":
        return "full"
    return "affected" if record.shards.get(str(shard)) else "empty"


def report_lines(record: Record, shard: int, shards: int) -> list[str]:
    mode = shard_mode(record, shard)
    lines = [f"PR TEST SELECTION: shard {shard} of {shards} runs mode={mode.upper()}"]
    lines.append(f"  reason: {record.reason}")
    lines.append(
        f"  {SWITCH_VARIABLE}={record.switch!r}, {QUEUE_SWITCH_VARIABLE}={record.queue_switch!r}, "
        f"event={record.event}, head={record.head_ref}"
    )
    if record.mode == "affected":
        mine = record.shards.get(str(shard), [])
        lines.append(
            f"  selected {len(record.selected_modules)} of {record.universe_modules} lane module(s) "
            f"for {len(record.changed_paths)} changed path(s), {len(record.declared_modules)} "
            f"declared in the PR body; this shard runs {len(mine)}"
        )
        lines += [f"    {module}" for module in mine]
        if record.demoted:
            lines.append(f"  {len(record.demoted)} weak-link module(s) not run at this head ({DEMOTE_VARIABLE})")
    if record.budget:
        lines.append("  " + summary_line(_fit_of(record)))
    if mode == "empty":
        lines.append(
            "  ZERO affected tests for this shard: it reports success without running pytest. "
            "The nightly FULL suites (ci.yml schedule, full-ci.yml) still run everything on main."
        )
    return lines


def summary_markdown(record: Record, shard: int, shards: int) -> str:
    mode = shard_mode(record, shard)
    rows = [
        f"### pytest fast lane (shard {shard} of {shards}): selection mode **{mode.upper()}**",
        "",
        "| field | value |",
        "|:--|:--|",
        f"| reason | {record.reason} |",
        f"| `{SWITCH_VARIABLE}` / `{QUEUE_SWITCH_VARIABLE}` | `{record.switch}` / `{record.queue_switch}` |",
        f"| event / head | `{record.event}` / `{record.head_ref}` |",
        f"| changed paths | {len(record.changed_paths)} |",
        f"| selected modules (all shards) | {len(record.selected_modules)} of {record.universe_modules} |",
        f"| declared in the PR body (`CI-Tests:`) | {len(record.declared_modules)} |",
        f"| this shard's modules | {len(record.shards.get(str(shard), []))} |",
        f"| weak-link modules not run at this head | {len(record.demoted)} |",
    ]
    if record.budget:
        rows.append(f"| test budget | {summary_line(_fit_of(record))} |")
    rows.append("")
    if mode == "empty":
        rows.append(
            "Zero affected tests for this shard, so pytest did not start. The nightly full "
            "suites (ci.yml schedule, full-ci.yml) still run everything on main."
        )
    elif mode == "affected":
        rows.append("<details><summary>this shard's modules</summary>\n")
        rows += [f"- `{m}`" for m in record.shards[str(shard)]]
        rows.append("\n</details>")
    return "\n".join(rows) + "\n"


# ---------------------------------------------------------------------------
# JUnit evidence for exit 5
# ---------------------------------------------------------------------------


def _dotted(module_path: str) -> str:
    return module_path.removesuffix(".py").replace("/", ".")


def junit_all_skipped(report: Path, modules: list[str]) -> bool:
    """True only when EVERY selected module skipped itself at module level, and nothing else ran.

    pytest exits 5 when it collects no test items. A module that skips itself at module level
    (`pytest.skip(..., allow_module_level=True)`) collects none and leaves one testcase named
    by its dotted path with a <skipped> child; a module with no tests leaves nothing at all.
    So exit 5 is excused only when each selected module has that skipped testcase and the
    report holds no other outcome, which is what the full lane reports green for them.
    """
    if not modules:
        return False
    cases = ElementTree.parse(report).getroot().findall(".//testcase")
    if not cases or not all(case.find("skipped") is not None for case in cases):
        return False
    skipped_modules = {case.get("name", "") for case in cases if not case.get("classname")}
    return all(_dotted(module) in skipped_modules for module in modules)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _append(path_env: str, text: str) -> None:
    target = os.environ.get(path_env)
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text)


def _select(arguments: argparse.Namespace) -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    payload = json.loads(Path(event_path).read_text(encoding="utf-8")) if event_path else {}
    event = Event.from_payload(event_name, payload, os.environ.get("GITHUB_REPOSITORY", ""))
    switch = os.environ.get(SWITCH_VARIABLE)
    queue_switch = os.environ.get(QUEUE_SWITCH_VARIABLE)
    bundle_json = os.environ.get(BUNDLE_VARIABLE)
    main_switch = os.environ.get(MAIN_SWITCH_VARIABLE)
    budget: BudgetConfig | None = None
    # A run a switch or its event keeps FULL never selects, so it never reads the
    # budget: a missing or broken budget file must not take the off-switch down with it.
    selects = full_suite_reason(switch, event, queue_switch, main_switch) is None
    if arguments.budget and selects:
        budget_path = Path(arguments.budget)
        if not budget_path.is_absolute():
            budget_path = REPO / budget_path  # relative to the repo, like every other input
        try:
            budget = load_config(budget_path)
        except BudgetError as error:
            # An unreadable or infeasible budget fails the shard (ci.yml, runner-canary.yml): FULL is
            # the uncapped lane, so the cap does not fall back to it.
            print(f"TEST BUDGET UNREADABLE: {error}", file=sys.stderr)
            return 2
    try:
        record = decide(
            switch,
            event,
            REPO,
            arguments.ignore,
            arguments.shards,
            queue_switch,
            bundle_json,
            budget,
            by_name=os.environ.get(BY_NAME_VARIABLE),
            demote=os.environ.get(DEMOTE_VARIABLE),
            main_switch=main_switch,
            range_json=os.environ.get(MAIN_RANGE_VARIABLE),
            active_shards=arguments.active_shards,
        )
    except HardCapError as refused:
        print(f"[ERROR] HARD CAP REFUSED: {refused}", file=sys.stderr)
        return HARD_CAP_REFUSED_RC

    out_dir = Path(arguments.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / RECORD_NAME).write_text(json.dumps(asdict(record), indent=2) + "\n", encoding="utf-8")
    mode = shard_mode(record, arguments.shard)
    if mode == "affected":
        (out_dir / SHARD_MODULES_NAME).write_text(
            "\n".join(record.shards[str(arguments.shard)]) + "\n", encoding="utf-8"
        )
    for line in report_lines(record, arguments.shard, arguments.shards):
        print(line, file=sys.stderr)
    _append("GITHUB_STEP_SUMMARY", summary_markdown(record, arguments.shard, arguments.shards))
    print(mode)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select", help="decide this shard's mode and modules")
    select.add_argument("--shard", type=int, required=True)
    select.add_argument("--shards", type=int, required=True)
    select.add_argument("--out-dir", required=True)
    select.add_argument("--ignore", action="append", default=[], help="a lane --ignore path")
    select.add_argument(
        "--budget", default=None, help="runner-minute budget config (ci/test-budget.yml); none = unbudgeted"
    )
    select.add_argument(
        "--active-shards", type=int, default=None,
        help="shards this run launched (CI-DYNSHARD); unset plans the count from the selection",
    )  # fmt: skip
    matrix = commands.add_parser("shard-matrix", help="print the JSON shard list the scope job launches")
    matrix.add_argument("--record", required=True, help="the scope's selection.json")
    matrix.add_argument("--max", type=int, required=True, help="the most shards a run may launch")
    junit = commands.add_parser("junit-all-skipped", help="exit 0 iff every selected module skipped at module level")
    junit.add_argument("report")
    junit.add_argument("--modules", required=True, help="the shard's shard-modules.txt")
    arguments = parser.parse_args(argv)
    if arguments.command == "select":
        if not 1 <= arguments.shard <= arguments.shards:
            parser.error(f"--shard {arguments.shard} is outside 1..{arguments.shards}")
        if arguments.active_shards is not None and not arguments.shard <= arguments.active_shards <= arguments.shards:
            parser.error(f"--active-shards {arguments.active_shards} is outside {arguments.shard}..{arguments.shards}")
        return _select(arguments)
    if arguments.command == "shard-matrix":
        print(json.dumps(shard_matrix(Path(arguments.record), arguments.max), separators=(",", ":")))
        return 0
    if arguments.command == "junit-all-skipped":
        modules = Path(arguments.modules).read_text(encoding="utf-8").split()
        return 0 if junit_all_skipped(Path(arguments.report), modules) else 1
    parser.error(f"unknown command {arguments.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
