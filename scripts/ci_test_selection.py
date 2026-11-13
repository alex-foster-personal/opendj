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
import os
import sys
from dataclasses import asdict
from pathlib import Path

from scripts.affected_tests import REPO
from scripts.ci_test_budget import BudgetConfig, BudgetError, load_config
from scripts.ci_test_selection_decide import HARD_CAP_REFUSED_RC, HardCapError, Record, decide, must_keep_guards
from scripts.ci_test_selection_declarations import (
    _DECLARATION,
    _DECLARATION_LINE,
    bundle_record_problem,
    declared_tests,
    main_push_range,
    parse_declarations,
    split_frontend_declarations,
    split_lane_ignored_declarations,
    undeclarable,
)
from scripts.ci_test_selection_rules import DURATIONS_NAME
from scripts.ci_test_selection_event import (
    BY_NAME_VARIABLE,
    BUNDLE_VARIABLE,
    DEMOTE_VARIABLE,
    Event,
    MAIN_RANGE_VARIABLE,
    MAIN_SWITCH_VARIABLE,
    QUEUE_SWITCH_VARIABLE,
    SCHEMA,
    SWITCH_VARIABLE,
    full_suite_reason,
    is_queue_draft,
    policy_from,
    RECORD_NAME,
    SHARD_MODULES_NAME,
)
from scripts.ci_test_selection_paths import ADDED_FILTER, range_changed_paths
from scripts.ci_test_selection_report import (
    junit_all_skipped,
    report_lines,
    shard_mode,
    summary_markdown,
)
from scripts.ci_test_selection_shards import (
    SHARD_TARGET_SECONDS,
    assign_shards,
    module_durations,
    planned_shard_count,
    shard_matrix,
)

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
            durations_path=Path(arguments.durations) if arguments.durations else None,
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
    select.add_argument(
        "--durations", default=None,
        help="the durations ledger to price and split on (CIPAR-12899); unset reads the checkout's .test_durations",
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
