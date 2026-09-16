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

Usage:
    python -m scripts.ci_miss_audit --runs 30 --main-runs 20
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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
from scripts.ci_plan import Change, Config, Plan, PlanError, Verdict, matches, plan, read_config

LEDGER_PATH = Path(__file__).resolve().parent.parent / ".test_durations"
FAST_CEILING_S = 0.5
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
    changes: tuple[Change, ...]
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
    plan_missed: list[str] = field(default_factory=list)
    fast_missed: list[str] = field(default_factory=list)

    @property
    def plan_recall(self) -> float | None:
        return None if self.pr_caused == 0 else 1 - len(self.plan_missed) / self.pr_caused

    @property
    def fast_recall(self) -> float | None:
        measured = self.pr_caused - self.tier_unknown
        return None if measured == 0 else 1 - len(self.fast_missed) / measured


# ----- pure -----


def selection_of(identity: str, run_plan: Plan) -> Selection:
    if run_plan.verdict is Verdict.FULL:
        return Selection.SELECTED
    if run_plan.verdict is Verdict.SKIP_PYTEST:
        return Selection.MISSED
    file_path = identity.split("::", 1)[0]
    return Selection.SELECTED if matches(file_path, run_plan.test_paths) else Selection.MISSED


def tier_of(identity: str, ledger: dict[str, float], ceiling: float) -> Tier:
    if "[" in identity and not identity.endswith("]"):
        return Tier.UNKNOWN  # the log parser stopped at whitespace inside the parameter id
    seconds = ledger.get(identity)
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
) -> Audit:
    result = Audit()
    for run in runs:
        caused = tuple(sorted(run.identities - trunk_red))
        red = tuple(sorted(run.identities & trunk_red))
        verdict: Verdict | None
        try:
            run_plan = plan(run.changes, config)
            verdict, reason = run_plan.verdict, run_plan.reason
        except PlanError as exc:
            run_plan, verdict, reason = None, None, f"plan UNKNOWN: {exc}"
        label = f"run {run.run_id} (PR #{run.pr})" if run.pr else f"run {run.run_id}"
        plan_missed = tuple(
            i
            for i in caused
            if run_plan is not None and selection_of(i, run_plan) is Selection.MISSED
        )
        tiers = {i: tier_of(i, ledger, ceiling) for i in caused}
        fast_missed = tuple(i for i in caused if tiers[i] is Tier.SLOW)
        unknown = tuple(i for i in caused if tiers[i] is Tier.UNKNOWN)
        result.rows.append(
            Row(run, verdict, reason, caused, red, plan_missed, fast_missed, unknown)
        )
        result.pr_caused += len(caused)
        result.trunk_red += len(red)
        result.tier_unknown += len(unknown)
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
        compare = _json(f"repos/{REPO}/compare/main...{run['head_sha']}")
        prs = run.get("pull_requests") or []
        out.append(
            RunFailure(
                run_id=run["id"],
                pr=prs[0]["number"] if prs else None,
                head=run["head_sha"],
                identities=identities,
                changes=changes_from_compare(compare.get("files") or []),
                unmeasured_jobs=unreadable,
            )
        )
        print(
            f"{LINE} read run {run['id']} head {run['head_sha'][:9]}: "
            f"{len(identities)} identities, {unreadable} unreadable pytest job(s)",
            file=sys.stderr,
        )
    return out


def trunk_red_in_window(
    main_runs: list[dict],
    fetch: Callable[[int], str] = _cached_log,
    sleep: Callable[[float], None] = time.sleep,
) -> frozenset[str]:
    red: set[str] = set()
    for run in main_runs:
        identities, _ = _pytest_identities(run, fetch, sleep)
        red |= identities
    return frozenset(red)


# ----- report -----


def _fraction(hit: int, denominator: int) -> str:
    if not denominator:
        return "UNKNOWN (denominator 0)"
    return f"{hit} / {denominator} = {hit / denominator:.3f}"


def report(result: Audit, *, runs_seen: int) -> None:
    measured = sum(1 for r in result.rows if r.pr_caused or r.trunk_red)
    unmeasured_jobs = sum(r.run.unmeasured_jobs for r in result.rows)
    print(
        f"{LINE} failed pull_request runs seen={runs_seen} with pytest identities={measured} "
        f"unreadable pytest jobs={unmeasured_jobs}"
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
        f"{LINE} identities: pr_caused={result.pr_caused} (the denominator) "
        f"trunk_red={result.trunk_red} tier_unknown={result.tier_unknown}"
    )
    planned = result.pr_caused - len(result.plan_missed)
    print(f"{LINE} plan recall      = {_fraction(planned, result.pr_caused)}")
    tiered = result.pr_caused - result.tier_unknown
    print(f"{LINE} fast-tier recall = {_fraction(tiered - len(result.fast_missed), tiered)}")
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
    args = parser.parse_args(argv)

    ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
    config = read_config()
    pr_runs = _failed_pr_runs(args.runs)
    runs = collect(pr_runs)
    trunk_red = trunk_red_in_window(_failed_main_runs(args.main_runs))
    result = audit(runs, trunk_red=trunk_red, config=config, ledger=ledger, ceiling=args.ceiling)
    report(result, runs_seen=len(pr_runs))
    if result.pr_caused == 0 and result.trunk_red == 0:
        print(f"{LINE} UNKNOWN: no failed run measured a pytest identity", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
