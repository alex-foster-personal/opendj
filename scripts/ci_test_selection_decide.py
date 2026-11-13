"""Selection record and decide() for PR test selection."""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path

from scripts.ci_test_budget import (
    DECLARED_DISTANCE,
    BudgetConfig,
    fit,
)
from scripts.ci_test_selection_declarations import (
    add_declared,
    main_push_range,
    run_declarations,
    split_frontend_declarations,
    split_lane_ignored_declarations,
    undeclarable,
)
from scripts.ci_test_selection_event import (
    Event,
    SCHEMA,
    SWITCH_VARIABLE,
    full_suite_reason,
    policy_from,
)
from scripts.ci_test_selection_paths import (
    ADDED_FILTER,
    _git,
    main_push_base,
    pr_changed_paths,
    range_changed_paths,
)
from scripts.ci_test_selection_rules import (
    DURATIONS_NAME,
    FullSuite,
    Policy,
    Selection,
    always_run_modules,
    capped_full_selection,
    lane_universe,
    select_tests,
)
from scripts.ci_test_selection_shards import assign_shards, module_durations, planned_shard_count


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


def _changed_paths(root: Path, event: Event, range_record: dict) -> tuple[list[str], list[str], str | None, str]:
    """(changed paths, the added ones among them, main push base or None, why that base) for this run."""
    if event.main_push:
        base, since = main_push_base(root, event.head_sha, range_record)
        return range_changed_paths(root, base), range_changed_paths(root, base, ADDED_FILTER), base, since
    return pr_changed_paths(root, event.head_sha), pr_changed_paths(root, event.head_sha, ADDED_FILTER), None, ""


def must_keep_guards(root: Path, pinned: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    """Modules the hard cap never drops: the derived-block guards plus every `ci/pr-always-run.txt` entry.

    Gate parity (issue #11103 class): a main push diffs every file since main's last green, so its
    selection overflowed the cap and dropped the always-run guards a PR head keeps. Main at
    f6a88de1cf went green on push while PRs went red on (private path).
    """
    return tuple(dict.fromkeys([guard for guard, _ in pinned] + always_run_modules(root)))


def _derived_block_pins(root: Path, changed: list[str], base: str | None) -> tuple[tuple[str, str], ...]:
    """The derived-block guards this diff can move (scripts/ci_derived_inputs.py, issue #11975),
    read against `base`, or the PR merge commit's merge base when `base` is None."""
    # Deferred like `load_config`: the scope-job helpers import this module under a bare python3
    # with no PyYAML, and only a selection reads ci/test-scopes.yml.
    try:
        from scripts.ci_derived_inputs import pinned_derived_guards
    except ImportError as unavailable:
        raise FullSuite(
            f"the derived-block reader needs PyYAML, not importable under {sys.executable}: {unavailable}"
        ) from unavailable
    diff_base = base if base is not None else _git(root, "merge-base", "HEAD^1", "HEAD^2")
    return tuple(pinned_derived_guards(root, changed, diff_base))


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
    added: list[str] | None = None,
    pinned: tuple[tuple[str, str], ...] = (),
) -> tuple[Selection, str | None]:
    """The narrowed selection, or under a hard cap the ranked whole lane with the reason it could not narrow.

    A hard cap has no full suite to fall back to (TEST-BUDGET round 5): the lane is ranked and
    cut instead, and the trunk checkpoint runs everything. Without a hard cap, or on an empty
    diff (indistinguishable from a failed one), the FullSuite propagates, and `decide` turns it into
    HardCapError under a hard cap. A main push's empty diff (a revert pair) is a real answer,
    not a failed read, so it ranks the lane like any unnarrowable change.
    """
    try:
        return select_tests(changed, ignores, root, policy, added=added, pinned=pinned), None
    except FullSuite as unnarrowable:
        if budget is None or not budget.hard or not (changed or main_push):
            raise
        return capped_full_selection(changed, ignores, root, added, pinned), str(unnarrowable)


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
    durations_path: Path | None = None,
) -> Record:
    """The whole decision for this run. Any failure is a FULL record, except under a hard cap.

    Under a hard budget a failure is HardCapError instead: FULL is the uncapped lane, so it is
    not an answer the cap can give. The switches (`full_suite_reason`) still keep a run FULL.

    `bundle_json` is CI_BUNDLE_DECLARATIONS; it is read only for a `trunk-merge/` head.
    `active_shards` is the number of shards the run launched (the scope job's plan); None plans it
    here from the selection's recorded seconds (`planned_shard_count`), which is what the scope does.
    `budget` trims an affected selection to its runner-minute budget (ci/test-budget.yml); a main
    push is priced as a PR head. `range_json` is CI_MAIN_PUSH_RANGE, read only for a main push.
    `durations_path` is the ledger that prices and splits the selection, `root/.test_durations` when
    None. The scope job passes the ledger the shards will install, so both price one selection
    from the same seconds (CIPAR-12899).
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
    declared, frontend_declared = split_frontend_declarations(declared, root)
    for frontend_path in frontend_declared:
        print(
            f"CI-Tests: {frontend_path} is not a pytest target (frontend); ignored for pytest selection",
            file=sys.stderr,
        )
    declared, lane_ignored = split_lane_ignored_declarations(declared, ignores, root)
    for ignored_path, ignored_by in lane_ignored:
        print(
            f"CI-Tests: {ignored_path} is lane-ignored for the fast lane ({ignored_by}); ignored for pytest selection",
            file=sys.stderr,
        )
    if lane_ignored and not declared and not frontend_declared and not errors:
        print(
            "CI-Tests: every declared path is ignored for pytest selection; selecting from the diff as if no CI-Tests line were present",
            file=sys.stderr,
        )
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
        rerun_hint = (
            " (after fixing the PR body, push an empty commit or close and reopen the PR; a rerun reuses the old body)"
            if problem.startswith("invalid declaration")
            else ""
        )
        _full_or_refuse(hard, f"cannot honor the hard cap: {problem}{rerun_hint}")
        return record("full", f"fell back to the full suite: {problem}")
    capped_over: str | None = None
    try:
        policy = policy_from(by_name, demote, root)
        changed, added, base, since = _changed_paths(root, event, range_record)
        pinned = _derived_block_pins(root, changed, base)
        selection, capped_over = _select_or_cap(changed, ignores, root, budget, policy, event.main_push, added, pinned)
        universe = lane_universe(selection.graph_paths, ignores, root)
        add_declared(selection, declared)
        ledger = json.loads((durations_path or root / DURATIONS_NAME).read_text(encoding="utf-8"))
        durations = module_durations(ledger)
        budget_block: dict | None = None
        if budget is not None:
            fitted = fit(
                selection.modules, selection.reasons, durations,
                budget.for_context(event.queue_draft), budget, selection.distance,
                must_keep_guards(root, pinned),
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
