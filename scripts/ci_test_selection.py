"""Which tests a pull request HEAD runs in the five pytest fast lane shards (DEVOPS-20).

the maintainer, Thu 1 Oct 2026, answered in chat: "Go now, skip the trial". A PR head runs only the
tests its change affects; every main push, every `workflow_dispatch` and every
`ci:trunk-repair` PR keep running the FULL suite. Decision record:
(private path).

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
  2. the event is `pull_request`;
  3. the head ref does not start with `trunk-merge/` (a Trunk queue draft), unless repo
     variable `CI_QUEUE_TEST_SELECTION` also equals `affected`;
  4. the PR does not carry the `ci:trunk-repair` label.

A PR head then falls back to FULL, never to an empty run, when a changed path is global
(`GLOBAL_PATTERNS`), when the import graph cannot resolve a changed Python path (the
UNKNOWN of `scripts.affected_tests`), when changed code is imported by a top-level
conftest, or when the selection cannot be computed for any reason at all. Every fallback
names its cause in the log, the job summary and the record.

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
from scripts.ci_test_budget import BudgetConfig, BudgetError, fit, load_config, summary_line
from scripts.ci_test_selection_rules import (
    DURATIONS_NAME,
    FullSuite,
    Policy,
    Selection,
    lane_universe,
    select_tests,
)

SWITCH_VARIABLE = "CI_PR_TEST_SELECTION"
QUEUE_SWITCH_VARIABLE = "CI_QUEUE_TEST_SELECTION"
BUNDLE_VARIABLE = "CI_BUNDLE_DECLARATIONS"
SWITCH_ON = "affected"
#: A Trunk queue draft is classified once, here, and the bundle collector imports it (Sol,
#: PR #7598): the head ref is `trunk-merge/pr-N/...` or `trunk-temp/pr-N/...`, the author is
#: the Trunk App, and the head lives in this repository. A branch name alone is
#: caller-controlled, so a human's `trunk-merge/x` branch is an ordinary PR head.
QUEUE_DRAFT_HEAD = re.compile(r"^trunk-(?:merge|temp)/pr-\d+/")
TRUNK_LOGIN = "trunk-io[bot]"
REPAIR_LABEL = "ci:trunk-repair"
BY_NAME_VARIABLE = "CI_PR_SELECT_BY_NAME"
BY_NAME_ON = "ci-config"
DEMOTE_VARIABLE = "CI_PR_DEMOTE_WEAK_LINKS"
#: Schema 2 (Tue 27 Oct 2026) adds `queue_switch`, `declared_modules`, `universe_modules` and
#: `bundle_prs`. Schema 3 (Fri 30 Oct 2026, TEST-BUDGET) adds `budget`: the runner-minute
#: budget the run was fitted to, the planned minutes before and after, and the weak-link
#: modules it dropped (scripts/ci_test_budget.py); None when `select` ran without `--budget`.
#: Schema 4 adds `policy` and `demoted` (TEST-CUT items 2 and 3, (private path)).
SCHEMA = 4
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

    @classmethod
    def from_payload(cls, name: str, payload: dict, repository: str) -> Event:
        """`repository` is GITHUB_REPOSITORY, which authenticates a queue draft's head."""
        pull_request = payload.get("pull_request") or {}
        head = pull_request.get("head") or {}
        return cls(
            name=name,
            head_ref=str(head.get("ref") or ""),
            head_sha=str(head.get("sha") or ""),
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


def full_suite_reason(switch: str | None, event: Event, queue_switch: str | None = None) -> str | None:
    """Why this run takes the full suite, or None when it is a PR head with selection on.

    `queue_switch` is `CI_QUEUE_TEST_SELECTION`; None is the variable unset, which keeps a
    queue draft on the full suite.
    """
    if event.name != "pull_request":
        return f"event `{event.name}` is not a pull request head; only PR heads select"
    if event.queue_draft and queue_switch != SWITCH_ON:
        return (
            f"head `{event.head_ref}` is a Trunk merge-queue draft; the queue runs everything "
            f"while repo variable {QUEUE_SWITCH_VARIABLE}={queue_switch!r} is not {SWITCH_ON!r}"
        )
    if REPAIR_LABEL in event.labels:
        return f"the PR carries `{REPAIR_LABEL}`; a trunk repair runs everything"
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


def run_declarations(event: Event, bundle_json: str | None) -> tuple[list[str], list[int], list[str]]:
    """(declared modules, bundle PRs, declaration errors) for this run; FullSuite when unreadable."""
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
        selection.add(path, "declared in the PR body (`CI-Tests:`)")
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


def assign_shards(modules: list[str], durations: dict[str, float], shards: int) -> list[list[str]]:
    """Greedy least-loaded assignment, heaviest module first; ties go to the lower shard.

    A module the ledger has never timed (a new test file) weighs the median recorded module,
    so it is neither free nor dominant.
    """
    known = [durations[m] for m in modules if m in durations]
    default = statistics.median(known) if known else 1.0
    weights = {m: durations.get(m, default) for m in modules}
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
) -> Record:
    """The whole decision for this run. Never raises: any failure is a FULL record.

    `bundle_json` is CI_BUNDLE_DECLARATIONS; it is read only for a `trunk-merge/` head.
    `budget` trims an affected selection to its runner-minute budget (ci/test-budget.yml).
    """
    changed: list[str] = []
    policy: Policy | None = None
    universe: list[str] = []
    declared: list[str] = []
    bundle_prs: list[int] = []
    errors: list[str] = []
    declarations_unknown: str | None = None
    try:
        declared, bundle_prs, errors = run_declarations(event, bundle_json)
    except FullSuite as unreadable:
        declarations_unknown = str(unreadable)
    errors = errors + [
        f"`CI-Tests:` path this lane never runs (typo, deleted, or lane-ignored): {path}"
        for path in undeclarable(declared, ignores, root)
    ]
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
        )

    reason = full_suite_reason(switch, event, queue_switch)
    if reason:
        return record("full", reason)
    if declarations_unknown:
        return record("full", f"fell back to the full suite: {declarations_unknown}")
    if declaration_error:
        return record("full", f"fell back to the full suite: invalid declaration: {declaration_error}")
    try:
        policy = policy_from(by_name, demote, root)
        changed = pr_changed_paths(root, event.head_sha)
        selection = select_tests(changed, ignores, root, policy)
        universe = lane_universe(selection.graph_paths, ignores, root)
        add_declared(selection, declared)
        ledger = json.loads((root / DURATIONS_NAME).read_text(encoding="utf-8"))
        durations = module_durations(ledger)
        budget_block: dict | None = None
        if budget is not None:
            fitted = fit(
                selection.modules, selection.reasons, durations,
                budget.for_context(event.queue_draft), budget,
            )  # fmt: skip
            selection.modules = fitted.kept
            budget_block = fitted.as_record()
        assignment = assign_shards(selection.modules, durations, shards)
    except FullSuite as fallback:
        return record("full", f"fell back to the full suite: {fallback}")
    except Exception as error:  # reported loudly below, and the fallback runs MORE tests
        traceback.print_exc(file=sys.stderr)
        return record("full", f"fell back to the full suite: selection could not be computed: {error!r}")
    demoted = f"; {len(selection.demoted)} weak-link module(s) not run at this head" if selection.demoted else ""
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
            "The nightly FULL suite (full-ci.yml) still runs everything on main."
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
        "",
    ]
    if record.budget:
        rows.append(f"| test budget | {summary_line(_fit_of(record))} |")
    rows.append("")
    if mode == "empty":
        rows.append(
            "Zero affected tests for this shard, so pytest did not start. The nightly full "
            "suite (full-ci.yml) still runs everything on main."
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
    budget: BudgetConfig | None = None
    if arguments.budget:
        budget_path = Path(arguments.budget)
        if not budget_path.is_absolute():
            budget_path = REPO / budget_path  # relative to the repo, like every other input
        try:
            budget = load_config(budget_path)
        except BudgetError as error:
            # A typo in the budget must make the run BIGGER, never silently smaller: a non-zero
            # exit is FULL, loudly, in the shard step.
            print(f"TEST BUDGET UNREADABLE: {error}", file=sys.stderr)
            return 2
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
    )

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
    junit = commands.add_parser("junit-all-skipped", help="exit 0 iff every selected module skipped at module level")
    junit.add_argument("report")
    junit.add_argument("--modules", required=True, help="the shard's shard-modules.txt")
    arguments = parser.parse_args(argv)
    if arguments.command == "select":
        if not 1 <= arguments.shard <= arguments.shards:
            parser.error(f"--shard {arguments.shard} is outside 1..{arguments.shards}")
        return _select(arguments)
    if arguments.command == "junit-all-skipped":
        modules = Path(arguments.modules).read_text(encoding="utf-8").split()
        return 0 if junit_all_skipped(Path(arguments.report), modules) else 1
    parser.error(f"unknown command {arguments.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
