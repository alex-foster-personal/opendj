"""Miss audit: would the planner have run the tests a pull request actually broke?

SMARTEST-CI round 7 (specs/ci-fail-fast.md, part 5). Selection stays a measurement until this
audit produces a recall number, so this REPORTS and never gates. For every failed `ci.yml`
pull_request run in the window it reads the failing pytest identities from the job logs (the
watcher's own reader, `scripts.ci_main_red.read_job_log`, so an empty read is UNMEASURED and
never "no failures"), subtracts every identity that also failed on main in the window (trunk
red is not the pull request's fault), plans the run's diff with `scripts.ci_plan.plan`, and
asks two questions per remaining identity:

    plan recall       would a SCOPED or SKIP_PYTEST plan have run it? (FULL always does)
    fast-tier recall  is it in the fast tier, i.e. under the ledger ceiling or unseen?

Both are printed as `hit / denominator = fraction`, each miss is named with its run and pull
request, and the denominator is stated (`pr_caused`), per the honest-denominator rule.

Exit codes: 0 measured; 3 UNKNOWN (no failed run in the window measured a single identity,
or every log was unreadable), never a recall number from absence.

MINI-PRD
    R1 [if] an identity's file lies under a SCOPED plan's test path [then] SELECTED, [else]
       MISSED [else stop] ✔︎ ✅ 🎯
    R2 [if] the plan is FULL [then] every identity is SELECTED; [if] SKIP_PYTEST [then] every
       identity is MISSED [else stop] ✔︎ ✅ 🎯
    R3 [if] the ledger records an identity under the ceiling, or never saw it [then] FAST;
       at or over [then] SLOW; a truncated parametrized id [then] UNKNOWN [else stop] ✔︎ ✅ 🎯
    R4 [if] an identity failed on main inside the window [then] it is trunk red and counts
       toward neither recall [else stop] ✔︎ ✅ 🎯
    R5 [if] no identity was measured [then] exit 3 and no fraction is printed [else stop]
       ✔︎ ✅ 🎯
    R6 [if] a GitHub compare status is not one this module maps [then] ValueError, never a
       guessed diff letter [else stop] ✔︎ ✅ 🎯
    R7 [if] either run listing is empty or its newest run is older than MAX_WINDOW_AGE
       [then] exit 3 naming the window, never a recall over a stale page [else stop] ✔︎ ✅ 🎯

Usage:
    python -m scripts.ci_miss_audit --runs 30 --main-runs 20
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from scripts.ci_failure_ids import INFRA_CLASS_JOBS
from scripts.ci_main_red import (
    REPO,
    LogUnreadable,
    _fetch_job_log,
    _jobs_of,
    _json,
    job_log_identities,
)
from scripts.ci_plan import (
    Change,
    Config,
    Plan,
    PlanError,
    Verdict,
    matches,
    plan,
    read_changes,
    read_config,
)
from scripts.review_gh import _gh

LEDGER_PATH = Path(__file__).resolve().parent.parent / ".test_durations"
ALWAYS_PATH = Path(__file__).resolve().parent.parent / "ci" / "fast-tier-always.txt"
FAST_CEILING_S = 0.5
# The runs listing has served a days-old page (Mon 21 Sep 2026: early-September runs, then
# current ones fifteen minutes later), and nothing downstream can tell. Failed pull request
# runs arrive every few hours on this fleet, so a newest run older than this is a bad page.
MAX_WINDOW_AGE = timedelta(hours=48)
CACHE_DIR = Path.home() / ".cache" / "opendj" / "ci-miss-audit"
PYTEST_JOBS = INFRA_CLASS_JOBS  # `pytest fast lane` shards and, since 6a, `pytest fast tier` legs
_COMPARE_LETTERS = {
    "added": "A",
    "modified": "M",
    "changed": "M",
    "removed": "D",
    "renamed": "R",
    "copied": "C",
}
LINE = "[miss-audit]"


_IDENTITY_PREFIX = re.compile(r"^(?:FAILED|ERROR) ")


def node_id(identity: str) -> str:
    """The pytest node id inside a parsed identity: `scripts.ci_failure_ids` keeps the
    `FAILED ` / `ERROR ` prefix so a log line stays distinguishable from a Playwright or TAP
    identity, while the ledger and the planner's test paths start at `tests/`. Compared
    unstripped, every failure misses the ledger and reads as an unseen fast test (Codex P1)."""
    return _IDENTITY_PREFIX.sub("", identity)


class Selection(StrEnum):
    SELECTED = "SELECTED"
    MISSED = "MISSED"


class Tier(StrEnum):
    FAST = "FAST"
    SLOW = "SLOW"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RunFailure:
    run_id: int
    pr: int | None
    head: str
    identities: frozenset[str]
    changes: tuple[Change, ...] | None  # None: the diff could not be read, plan UNKNOWN
    unmeasured_jobs: int


@dataclass(frozen=True)
class Row:
    run: RunFailure
    verdict: Verdict | None
    reason: str
    pr_caused: tuple[str, ...]
    trunk_red: tuple[str, ...]
    plan_missed: tuple[str, ...]
    fast_missed: tuple[str, ...]
    tier_unknown: tuple[str, ...]


@dataclass
class Audit:
    rows: list[Row] = field(default_factory=list)
    pr_caused: int = 0
    trunk_red: int = 0
    tier_unknown: int = 0
    plan_unmeasured: int = 0  # PR-caused identities in runs whose plan is UNKNOWN
    unreadable_runs: int = 0  # PR runs with an unreadable failed pytest job: excluded whole
    unreadable_identities: int = 0  # what those runs did name, kept out of every count
    trunk_unreadable_jobs: int = 0  # unreadable failed pytest jobs on main: trunk red incomplete
    plan_missed: list[str] = field(default_factory=list)
    fast_missed: list[str] = field(default_factory=list)

    @property
    def verdicts(self) -> dict[str, int]:
        """How many runs got each verdict. Plan recall is VACUOUS while SCOPED is 0: FULL
        selects everything by construction, so 1.0 over FULL-only runs measures nothing."""
        counts: dict[str, int] = {}
        for row in self.rows:
            key = row.verdict.value if row.verdict is not None else "UNKNOWN"
            counts[key] = counts.get(key, 0) + 1
        return counts

    @property
    def plan_denominator(self) -> int:
        return self.pr_caused - self.plan_unmeasured

    @property
    def fast_denominator(self) -> int:
        return self.pr_caused - self.tier_unknown

    @property
    def plan_recall(self) -> float | None:
        """None is UNKNOWN: nothing measured, or main's red set was not fully read, so an
        identity counted as PR-caused may be trunk red."""
        if self.trunk_unreadable_jobs or self.plan_denominator == 0:
            return None
        return 1 - len(self.plan_missed) / self.plan_denominator

    @property
    def fast_recall(self) -> float | None:
        if self.trunk_unreadable_jobs or self.fast_denominator == 0:
            return None
        return 1 - len(self.fast_missed) / self.fast_denominator


# ----- pure -----


def selection_of(identity: str, run_plan: Plan) -> Selection:
    if run_plan.verdict is Verdict.FULL:
        return Selection.SELECTED
    if run_plan.verdict is Verdict.SKIP_PYTEST:
        return Selection.MISSED
    file_path = node_id(identity).split("::", 1)[0]
    return Selection.SELECTED if matches(file_path, run_plan.test_paths) else Selection.MISSED


def tier_of(
    identity: str,
    ledger: dict[str, float],
    ceiling: float,
    always: frozenset[str] = frozenset(),
) -> Tier:
    node = node_id(identity)
    if any(node == entry or node.startswith(entry + "::") for entry in always):
        return Tier.FAST  # forced by ci/fast-tier-always.txt, whatever the ledger says
    if "[" in identity and not identity.endswith("]"):
        return Tier.UNKNOWN  # the log parser stopped at whitespace inside the parameter id
    seconds = ledger.get(node)
    if seconds is None:
        return Tier.FAST  # the tier includes every test the ledger has never seen
    return Tier.FAST if seconds < ceiling else Tier.SLOW


def changes_from_compare(files: list[dict]) -> tuple[Change, ...]:
    changes: list[Change] = []
    for entry in files:
        status = entry["status"]
        if status == "unchanged":
            continue
        if status not in _COMPARE_LETTERS:
            raise ValueError(
                f"unmapped GitHub compare status {status!r} for {entry.get('filename')!r}"
            )
        letter = _COMPARE_LETTERS[status]
        if letter == "R":
            changes.append(Change(letter, entry["previous_filename"]))
        changes.append(Change(letter, entry["filename"]))
    return tuple(changes)


def audit(
    runs: list[RunFailure],
    *,
    trunk_red: frozenset[str],
    config: Config,
    ledger: dict[str, float],
    ceiling: float,
    trunk_unreadable_jobs: int = 0,
    always: frozenset[str] = frozenset(),
) -> Audit:
    result = Audit(trunk_unreadable_jobs=trunk_unreadable_jobs)
    for run in runs:
        label = f"run {run.run_id} (PR #{run.pr})" if run.pr else f"run {run.run_id}"
        if run.unmeasured_jobs:
            # A failed pytest job this run could not read may hold exactly the slow or
            # unselected failure the audit exists to find: the whole run is unmeasured.
            result.unreadable_runs += 1
            result.unreadable_identities += len(run.identities)
            result.rows.append(
                Row(
                    run,
                    None,
                    f"UNMEASURED: {run.unmeasured_jobs} unreadable pytest job(s)",
                    (),
                    (),
                    (),
                    (),
                    (),
                )
            )
            continue
        caused = tuple(sorted(run.identities - trunk_red))
        red = tuple(sorted(run.identities & trunk_red))
        run_plan: Plan | None = None
        verdict: Verdict | None = None
        if run.changes is None:
            reason = "plan UNKNOWN: the diff could not be read"
        else:
            try:
                run_plan = plan(run.changes, config)
                verdict, reason = run_plan.verdict, run_plan.reason
            except PlanError as exc:
                reason = f"plan UNKNOWN: {exc}"
        plan_missed = tuple(
            i
            for i in caused
            if run_plan is not None and selection_of(i, run_plan) is Selection.MISSED
        )
        tiers = {i: tier_of(i, ledger, ceiling, always) for i in caused}
        fast_missed = tuple(i for i in caused if tiers[i] is Tier.SLOW)
        unknown = tuple(i for i in caused if tiers[i] is Tier.UNKNOWN)
        result.rows.append(
            Row(run, verdict, reason, caused, red, plan_missed, fast_missed, unknown)
        )
        result.pr_caused += len(caused)
        result.trunk_red += len(red)
        result.tier_unknown += len(unknown)
        if run_plan is None:
            result.plan_unmeasured += len(caused)
        result.plan_missed.extend(f"{label}: {i}" for i in plan_missed)
        result.fast_missed.extend(f"{label}: {i}" for i in fast_missed)
    return result


# ----- GitHub reads -----


def _cached_log(job_id: int) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"job-{job_id}.log"
    if path.exists() and path.stat().st_size > 0:
        return path.read_text(encoding="utf-8", errors="replace")
    log = _fetch_job_log(job_id)
    if log:
        path.write_text(log, encoding="utf-8")
    return log


def stale_window_reason(
    runs: list[dict], *, now: datetime, max_age: timedelta = MAX_WINDOW_AGE
) -> str | None:
    """Why a run listing cannot be the current window, or None when it can."""
    if not runs:
        return "the listing returned no runs"
    newest = max(datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) for r in runs)
    if now - newest > max_age:
        return f"newest run {newest:%Y-%m-%dT%H:%MZ} is older than {max_age} (stale page?)"
    return None


def _window(runs: list[dict]) -> str:
    stamps = sorted(r["created_at"] for r in runs)
    return f"{stamps[0]} .. {stamps[-1]}" if stamps else "empty"


def _failed_pr_runs(limit: int) -> list[dict]:
    payload = _json(
        f"repos/{REPO}/actions/workflows/ci.yml/runs?event=pull_request&status=failure&per_page={limit}"
    )
    return payload["workflow_runs"][:limit]


def _failed_main_runs(limit: int) -> list[dict]:
    payload = _json(
        f"repos/{REPO}/actions/workflows/ci.yml/runs?branch=main&status=failure&per_page={limit}"
    )
    return payload["workflow_runs"][:limit]


def _pytest_identities(
    run: dict, fetch: Callable[[int], str], sleep: Callable[[float], None]
) -> tuple[frozenset[str], int]:
    """Identities from every failed pytest job of the run's latest attempt, plus how many
    failed pytest jobs could not be read (each one is a hole in the measurement)."""
    identities: set[str] = set()
    unreadable = 0
    for job in _jobs_of(run["id"], run["run_attempt"]):
        if job.conclusion != "failure" or not PYTEST_JOBS.search(job.name):
            continue
        try:
            identities |= job_log_identities(job.job_id, fetch, sleep)
        except LogUnreadable:
            unreadable += 1
    return frozenset(identities), unreadable


def _pull_of(head: str) -> tuple[int | None, str | None]:
    """The pull request a run head belongs to and its merge commit, from the commit itself:
    a run's own `pull_requests` field is EMPTY once the pull request has closed."""
    pulls = _json_list(f"repos/{REPO}/commits/{head}/pulls")
    if not pulls:
        return None, None
    return pulls[0]["number"], pulls[0].get("merge_commit_sha") if pulls[0].get(
        "merged_at"
    ) else None


def _json_list(path: str) -> list[dict]:
    return json.loads(_gh(["api", path]))


def _changes_of(head: str, merge_sha: str | None) -> tuple[Change, ...] | None:
    """The run's diff against its branch point. A MERGED head is an ancestor of main, so the
    compare API answers `behind` with zero files; the merge commit's first parent is main at
    merge time, and `git diff` from there is the pull request's diff at that head."""
    if merge_sha:
        try:
            return read_changes(f"{merge_sha}^1", head)
        except PlanError:
            return None  # the object is not in this checkout: UNKNOWN, never an empty diff
    compare = _json(f"repos/{REPO}/compare/main...{head}")
    return changes_from_compare(compare.get("files") or [])


def collect(
    pr_runs: list[dict],
    fetch: Callable[[int], str] = _cached_log,
    sleep: Callable[[float], None] = time.sleep,
) -> list[RunFailure]:
    out: list[RunFailure] = []
    for run in pr_runs:
        identities, unreadable = _pytest_identities(run, fetch, sleep)
        if not identities and unreadable == 0:
            continue  # the run failed outside pytest (frontend, ratchet, e2e); nothing to audit
        pr, merge_sha = _pull_of(run["head_sha"])
        out.append(
            RunFailure(
                run_id=run["id"],
                pr=pr,
                head=run["head_sha"],
                identities=identities,
                changes=_changes_of(run["head_sha"], merge_sha),
                unmeasured_jobs=unreadable,
            )
        )
        print(
            f"{LINE} read run {run['id']} head {run['head_sha'][:9]} pr={pr}: "
            f"{len(identities)} identities, {unreadable} unreadable pytest job(s)",
            file=sys.stderr,
        )
    return out


def trunk_red_in_window(
    main_runs: list[dict],
    fetch: Callable[[int], str] = _cached_log,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[frozenset[str], int]:
    """Main's failing identities over the window, and how many failed pytest jobs could not
    be read. An unread main job means the subtraction is incomplete, so a PR-caused count
    built on it is UNKNOWN; the caller must not print a fraction over it."""
    red: set[str] = set()
    unreadable = 0
    for run in main_runs:
        identities, missing = _pytest_identities(run, fetch, sleep)
        red |= identities
        unreadable += missing
    return frozenset(red), unreadable


# ----- report -----


def _fraction(hit: int, denominator: int, *, valid: bool, why: str) -> str:
    if not valid:
        return f"UNKNOWN ({why})"
    if not denominator:
        return "UNKNOWN (denominator 0)"
    return f"{hit} / {denominator} = {hit / denominator:.3f}"


def report(result: Audit, *, runs_seen: int) -> None:
    measured = sum(1 for r in result.rows if r.pr_caused or r.trunk_red)
    print(
        f"{LINE} failed pull_request runs seen={runs_seen} measured={measured} "
        f"excluded for an unreadable pytest job={result.unreadable_runs} "
        f"(naming {result.unreadable_identities} identities)"
    )
    for row in result.rows:
        print(
            f"{LINE} run={row.run.run_id} pr={row.run.pr} head={row.run.head[:9]} "
            f"verdict={row.verdict} "
            f"pr_caused={len(row.pr_caused)} trunk_red={len(row.trunk_red)} "
            f"plan_missed={len(row.plan_missed)} fast_missed={len(row.fast_missed)} "
            f"tier_unknown={len(row.tier_unknown)} :: {row.reason}"
        )
    print(
        f"{LINE} identities: pr_caused={result.pr_caused} trunk_red={result.trunk_red} "
        f"plan_unmeasured={result.plan_unmeasured} tier_unknown={result.tier_unknown} "
        f"trunk_unreadable_jobs={result.trunk_unreadable_jobs}"
    )
    verdicts = " ".join(f"{k}={v}" for k, v in sorted(result.verdicts.items()))
    vacuous = (
        "" if result.verdicts.get("SCOPED") else "  (plan recall is VACUOUS: no run was narrowed)"
    )
    print(f"{LINE} plans: {verdicts}{vacuous}")
    trunk_ok = result.trunk_unreadable_jobs == 0
    why = f"{result.trunk_unreadable_jobs} unreadable main job(s): trunk red incomplete"
    planned = result.plan_denominator - len(result.plan_missed)
    print(
        f"{LINE} plan recall      = "
        f"{_fraction(planned, result.plan_denominator, valid=trunk_ok, why=why)}"
    )
    fast = result.fast_denominator - len(result.fast_missed)
    print(
        f"{LINE} fast-tier recall = "
        f"{_fraction(fast, result.fast_denominator, valid=trunk_ok, why=why)}"
    )
    for miss in result.plan_missed:
        print(f"{LINE} PLAN MISS  {miss}")
    for miss in result.fast_missed:
        print(f"{LINE} FAST MISS  {miss}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--runs", type=int, default=30, help="newest failed pull_request runs")
    parser.add_argument("--main-runs", type=int, default=20, help="failed main runs = trunk red")
    parser.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    parser.add_argument("--ceiling", type=float, default=FAST_CEILING_S)
    parser.add_argument(
        "--always",
        type=Path,
        default=ALWAYS_PATH,
        help="the always-fast list the tier plugin reads; pass /dev/null to measure without it",
    )
    args = parser.parse_args(argv)

    ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
    always = frozenset(
        entry
        for entry in (
            line.split("#", 1)[0].strip()
            for line in args.always.read_text(encoding="utf-8").splitlines()
        )
        if entry
    )
    config = read_config()
    pr_runs = _failed_pr_runs(args.runs)
    main_runs = _failed_main_runs(args.main_runs)
    now = datetime.now(UTC)
    for label, listed in (("pull_request", pr_runs), ("main", main_runs)):
        print(f"{LINE} {label} window: {_window(listed)} ({len(listed)} runs)", file=sys.stderr)
        if reason := stale_window_reason(listed, now=now):
            print(f"{LINE} UNKNOWN: {label} listing refused: {reason}", file=sys.stderr)
            return 3
    runs = collect(pr_runs)
    trunk_red, trunk_unreadable = trunk_red_in_window(main_runs)
    result = audit(
        runs,
        trunk_red=trunk_red,
        config=config,
        ledger=ledger,
        ceiling=args.ceiling,
        trunk_unreadable_jobs=trunk_unreadable,
        always=always,
    )
    print(f"{LINE} always-fast entries applied: {len(always)} from {args.always}")
    report(result, runs_seen=len(pr_runs))
    if result.fast_recall is None and result.plan_recall is None:
        print(f"{LINE} UNKNOWN: no recall could be measured", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
