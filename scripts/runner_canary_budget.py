"""Runner-canary budget gate: refuse a canary run past 80% of a vendor's free tier.

The first job of `.github/workflows/runner-canary.yml`. It sums this UTC calendar month's
billed minutes per vendor runner label from the GitHub Actions jobs API, each job rounded UP
to a whole minute and scaled from the label's vCPU count to the vCPU basis the vendor's free
tier is quoted in, and fails red with an explicit `::error::` at or past
`budget_gate_fraction` (0.8) of that tier. It also decides which vendors run at all: only
those named in `vars.CANARY_VENDORS_ENABLED`, and never a fallback runner for the rest.

Each vendor runs in exactly one repository, its config `target_repo`: Avrea and Tenki in
the source repository, where their apps are installed, and Blacksmith and Ubicloud in the
canary org's mirror, because their runners are organization-only. A vendor enabled in any
other repository is refused, and a repository that is no vendor's target runs nothing, so
a vendor's minutes are always summed in the one repository that can spend them.

Decision record: docs/decisions/ADR-NEW-runner-canary.md. Config: ci/runner-canary.json.
Standard library only: the gate job runs the hosted image's bare `python3`, with no venv.

Exit codes: 0 green (writes `vendors=` and `labels=` to $GITHUB_OUTPUT), 1 red (over budget,
disabled vendor, bad enable list, wrong repository owner, missing env), 2 UNKNOWN (the
billing read failed or was partial). Every non-zero exit fails the job, so UNKNOWN is red.

Requirements (mini-PRD)
- [if] a requested vendor's month-to-date basis minutes are at or past 80% of its free tier
  [then] exit 1 naming vendor, used, free and month, [else stop] ✔︎ ✅ 🎯
- [if] every requested vendor is under 80% [then] exit 0 and the outputs name exactly the
  requested vendors and their labels, [else stop] ✔︎ ✅ 🎯
- [if] the billing read errors, is paginated short, exceeds the listing cap, or holds a job
  with no labels list or, unskipped, no started_at [then] exit 2 UNKNOWN with no outputs,
  [else stop] ✔︎ ✅ 🎯
- [if] a job ran at any moment this month, even from a run created last month [then] it
  counts in full; [if] it finished before the 1st [then] it counts nothing, [else stop]
  ✔︎ ✅ 🎯
- [if] a vendor is not in the enable list [then] it is absent from the outputs, and
  dispatching it by name is exit 1, [else stop] ✔︎ ✅ 🎯
- [if] the enable list is empty, unset, malformed or names an unknown vendor [then] exit 1,
  [else stop] ✔︎ ✅ 🎯
- [if] the repository is no vendor's target_repo, or an enabled vendor targets another
  repository [then] exit 1 before any billing read, [else stop] ✔︎ ✅ 🎯
- [if] a vendor is evaluated [then] its minutes come from its own target_repo's jobs, and a
  target that was not read is UNKNOWN, [else stop] ✔︎ ✅ 🎯
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

#: GitHub's list endpoints stop returning rows past 1,000 results for a filtered query.
LISTING_CAP = 1000
PER_PAGE = 100
HTTP_TIMEOUT_S = 30
#: GitHub cancels a workflow run 35 days after it is created, waiting and approval included
#: (https://docs.github.com/en/actions/reference/limits), so a run created up to 35 days
#: before the 1st can still have a job running this month.
MAX_RUN_LIFETIME = timedelta(days=35)
CANARY_WORKFLOW_FILE = "runner-canary.yml"
PLANNED_VENDORS_TITLE = "runner-canary planned vendors"


class BudgetGateError(Exception):
    """A refusal the gate can name: over budget, disabled vendor, bad config. Exit 1."""


class BillingUnreadable(Exception):
    """The billing read could not be completed. UNKNOWN, exit 2, never green."""


# ----- config -----------------------------------------------------------------


@dataclass(frozen=True)
class Vendor:
    name: str
    label: str
    target_repo: str
    vcpu: int
    free_minutes_per_month: int
    free_minutes_vcpu_basis: int
    sources: tuple[str, ...]


@dataclass(frozen=True)
class CanaryConfig:
    source_repository: str
    mirror_owner: str
    mirror_repository: str
    budget_gate_fraction: float
    shard_count: int
    shard_timeout_minutes: int
    pytest_step_name: str
    vendors: dict[str, Vendor]
    baseline: dict[str, str]
    success_bars: dict[str, float]
    known_red_shas: tuple[str, ...]

    @property
    def target_repositories(self) -> tuple[str, ...]:
        return tuple(sorted({v.target_repo for v in self.vendors.values()}))

    @property
    def allowed_owners(self) -> tuple[str, ...]:
        """The owners the workflow's guard admits: exactly those of the target repositories."""
        return tuple(sorted({repo.split("/")[0] for repo in self.target_repositories}))


def load_config(path: Path) -> CanaryConfig:
    raw = json.loads(Path(path).read_text())
    vendors = {
        name: Vendor(
            name=name,
            label=v["label"],
            target_repo=v["target_repo"],
            vcpu=int(v["vcpu"]),
            free_minutes_per_month=int(v["free_minutes_per_month"]),
            free_minutes_vcpu_basis=int(v["free_minutes_vcpu_basis"]),
            sources=tuple(v["sources"]),
        )
        for name, v in raw["vendors"].items()
    }
    allowed_targets = (raw["source_repository"], raw["mirror_repository"])
    for vendor in vendors.values():
        if vendor.target_repo not in allowed_targets:
            raise BudgetGateError(
                f"{vendor.name} targets {vendor.target_repo!r}; a target_repo must be the "
                f"source or the mirror repository {allowed_targets}"
            )
    return CanaryConfig(
        source_repository=raw["source_repository"],
        mirror_owner=raw["mirror_owner"],
        mirror_repository=raw["mirror_repository"],
        budget_gate_fraction=float(raw["budget_gate_fraction"]),
        shard_count=int(raw["shard_count"]),
        shard_timeout_minutes=int(raw["shard_timeout_minutes"]),
        pytest_step_name=raw["pytest_step_name"],
        vendors=vendors,
        baseline=dict(raw["baseline"]),
        success_bars={k: float(v) for k, v in raw["success_bars"].items()},
        known_red_shas=tuple(raw["known_red_shas"]),
    )


# ----- enablement ---------------------------------------------------------------


def parse_enabled_vendors(raw: str, config: CanaryConfig) -> list[str]:
    """`vars.CANARY_VENDORS_ENABLED` as a JSON list of known vendor names, in config order."""
    if not raw.strip():
        raise BudgetGateError(
            "no vendors enabled: vars.CANARY_VENDORS_ENABLED is unset or empty; "
            'set it to a JSON list such as ["avrea"]'
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BudgetGateError(f"vars.CANARY_VENDORS_ENABLED is not JSON: {raw!r} ({exc})") from exc
    if not isinstance(parsed, list) or not all(isinstance(v, str) for v in parsed):
        raise BudgetGateError(f"vars.CANARY_VENDORS_ENABLED must be a JSON list of names: {raw!r}")
    if not parsed:
        raise BudgetGateError("no vendors enabled: vars.CANARY_VENDORS_ENABLED is []")
    unknown = sorted(set(parsed) - config.vendors.keys())
    if unknown:
        raise BudgetGateError(
            f"vars.CANARY_VENDORS_ENABLED names unknown vendor(s) {unknown}; "
            f"known: {sorted(config.vendors)}"
        )
    return [name for name in config.vendors if name in parsed]


def check_repository(owner: str, repository: str, config: CanaryConfig) -> None:
    """Refuse any repository that is not some vendor's target: it would run nothing, and its
    own jobs listing would read as a fresh, zero-minute budget."""
    if owner not in config.allowed_owners or repository not in config.target_repositories:
        raise BudgetGateError(
            f"{repository} (owner {owner!r}) runs no canary vendor; the targets are "
            f"{list(config.target_repositories)}"
        )


def require_vendors_target(repository: str, vendors: Iterable[str], config: CanaryConfig) -> None:
    """A vendor runs only in its target_repo: never skipped silently, never elsewhere."""
    misrouted = [
        f"{name} runs only in {config.vendors[name].target_repo}"
        for name in vendors
        if config.vendors[name].target_repo != repository
    ]
    if misrouted:
        raise BudgetGateError(
            "; ".join(misrouted) + f", not {repository}; remove it from this repository's "
            "vars.CANARY_VENDORS_ENABLED"
        )


def requested_vendors(requested: str, enabled: list[str]) -> list[str]:
    """The vendors this run covers: all enabled ones, or one enabled vendor by name."""
    if requested == "all":
        return list(enabled)
    if requested in enabled:
        return [requested]
    if requested:
        raise BudgetGateError(
            f"{requested} is not enabled (vars.CANARY_VENDORS_ENABLED = {enabled}); "
            "a disabled vendor never runs and is never rerouted to another runner"
        )
    raise BudgetGateError("requested vendor is unknown: CANARY_REQUESTED_VENDOR is empty")


# ----- billing arithmetic -------------------------------------------------------


def _parse_ts(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise BillingUnreadable(f"unparseable job timestamp {value!r}") from exc


def month_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, 1, tzinfo=UTC)


def listing_since(now: datetime) -> datetime:
    """The oldest run creation date that can still hold a job running this month."""
    return month_start(now) - MAX_RUN_LIFETIME


def billed_basis_minutes(job: dict[str, Any], vendor: Vendor, now: datetime) -> float:
    """One job's minutes against this month's free tier: rounded up, vCPU-scaled.

    Neither GitHub's billing docs nor the vendor pages say whether a job bills in the month
    it starts or the month it ends, so a job counts IN FULL against every month it ran in.
    That is right under either rule, and a straddling shard (30 minutes at most) is charged
    to both months, which errs toward refusing.
    """
    # A skipped job never had a runner. GitHub still stamps it, and stamps it BACKWARDS
    # (completed_at one second before started_at, measured on the source repository's
    # jobs API, Tue 29 Sep 2026: 13 of 13 skipped jobs in a 3-minute window).
    if job.get("conclusion") == "skipped":
        return 0
    started = job.get("started_at")
    if not isinstance(started, str):  # Sol P1 on b6303e8f0: unknown, never zero
        raise BillingUnreadable(
            f"job {job.get('id')} has no started_at, so its minutes are unknown"
        )
    start = _parse_ts(started)
    completed = job.get("completed_at")
    end = _parse_ts(completed) if completed else now
    seconds = (end - start).total_seconds()
    if seconds < 0:
        raise BillingUnreadable(f"job ends before it starts: {started} -> {completed}")
    if end < month_start(now):
        return 0
    minutes = max(1, math.ceil(seconds / 60))
    return minutes * vendor.vcpu / vendor.free_minutes_vcpu_basis


def _labels(job: dict[str, Any]) -> list[str]:
    """A job's runner labels. Without them, whose minutes it holds is unknown (Sol P1)."""
    labels = job.get("labels")
    if not isinstance(labels, list):
        raise BillingUnreadable(f"job {job.get('id')} has no labels list, so its vendor is unknown")
    return labels


@dataclass(frozen=True)
class VendorBudget:
    vendor: str
    used_minutes: float
    free_minutes: int
    limit_minutes: float

    @property
    def over(self) -> bool:
        return self.used_minutes >= self.limit_minutes


def evaluate_budgets(
    vendors: Iterable[str],
    jobs_by_repo: dict[str, list[dict[str, Any]]],
    config: CanaryConfig,
    now: datetime,
) -> list[VendorBudget]:
    """Each vendor's month against its tier, summed from its OWN target repository's jobs."""
    budgets = []
    for name in vendors:
        vendor = config.vendors[name]
        if vendor.target_repo not in jobs_by_repo:
            raise BillingUnreadable(f"{name}'s target {vendor.target_repo} was not read")
        jobs = jobs_by_repo[vendor.target_repo]
        used = sum(
            billed_basis_minutes(job, vendor, now) for job in jobs if vendor.label in _labels(job)
        )
        budgets.append(
            VendorBudget(
                vendor=name,
                used_minutes=used,
                free_minutes=vendor.free_minutes_per_month,
                limit_minutes=vendor.free_minutes_per_month * config.budget_gate_fraction,
            )
        )
    return budgets


# ----- the decision -------------------------------------------------------------


@dataclass(frozen=True)
class GateDecision:
    vendors: list[str]
    labels: dict[str, str]
    budgets: list[VendorBudget]

    def github_output_lines(self) -> list[str]:
        return [f"vendors={json.dumps(self.vendors)}", f"labels={json.dumps(self.labels)}"]

    def planned_vendors_notice(self) -> str:
        """A workflow command GitHub stores as an annotation on this gate job. The report
        reads it back to know which vendor shards the run owes, so a matrix job GitHub never
        created still counts (scripts/runner_canary_report.py PLANNED_VENDORS_TITLE)."""
        return f"::notice title={PLANNED_VENDORS_TITLE}::{','.join(self.vendors)}"


def select_vendors(
    *, owner: str, repository: str, enabled_raw: str, requested: str, config: CanaryConfig
) -> list[str]:
    """Which vendors this run covers. Pure, and decided before any billing read."""
    check_repository(owner, repository, config)
    enabled = parse_enabled_vendors(enabled_raw, config)
    require_vendors_target(repository, enabled, config)
    return requested_vendors(requested, enabled)


def gate_decision(
    *,
    owner: str,
    repository: str,
    enabled_raw: str,
    requested: str,
    jobs_by_repo: dict[str, list[dict[str, Any]]],
    config: CanaryConfig,
    now: datetime,
) -> GateDecision:
    vendors = select_vendors(
        owner=owner,
        repository=repository,
        enabled_raw=enabled_raw,
        requested=requested,
        config=config,
    )
    budgets = evaluate_budgets(vendors, jobs_by_repo, config, now)
    month = now.strftime("%Y-%m")
    refusals = [
        f"{b.vendor} used {b.used_minutes:g} of {b.free_minutes} free minutes in {month} "
        f"(limit {b.limit_minutes:g} at {config.budget_gate_fraction:.0%})"
        for b in budgets
        if b.over
    ]
    if refusals:
        raise BudgetGateError("; ".join(refusals) + "; canary refused")
    return GateDecision(
        vendors=vendors, labels={v: config.vendors[v].label for v in vendors}, budgets=budgets
    )


# ----- the GitHub read (imperative shell) --------------------------------------


def collect_paginated(fetch_page: Callable[[int], dict[str, Any]], items_key: str) -> list[Any]:
    """Every row of a GitHub list endpoint, or BillingUnreadable. Never a partial list."""
    items: list[Any] = []
    page = 1
    total: int | None = None
    while total is None or len(items) < total:
        body = fetch_page(page)
        if not isinstance(body.get("total_count"), int) or not isinstance(
            body.get(items_key), list
        ):
            raise BillingUnreadable(f"response has no total_count/{items_key}: {str(body)[:200]}")
        total = body["total_count"]
        if total > LISTING_CAP:
            raise BillingUnreadable(
                f"{total} {items_key} exceed the {LISTING_CAP}-row listing cap, "
                "so the sum would be partial"
            )
        if not body[items_key] and len(items) < total:
            raise BillingUnreadable(f"pagination ended at {len(items)} of {total} {items_key}")
        items.extend(body[items_key])
        page += 1
    return items


def _get_json(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise BillingUnreadable(f"GET {url} failed: {exc}") from exc


def runs_listing_url(api_url: str, repo: str, now: datetime, page: int) -> str:
    """One page of this workflow's runs that can hold a job running in `now`'s month."""
    created = listing_since(now).strftime("%Y-%m-%d")
    return (
        f"{api_url.rstrip('/')}/repos/{repo}/actions/workflows/{CANARY_WORKFLOW_FILE}/runs"
        f"?created=%3E%3D{created}&per_page={PER_PAGE}&page={page}"
    )


def fetch_month_jobs(api_url: str, repo: str, token: str, now: datetime) -> list[dict[str, Any]]:
    """Every job (all attempts) of every canary run in `repo` that can bill this month.

    Only this workflow's runs: no other workflow names a vendor label, which
    tests/scripts/test_runner_canary_workflow.py pins.
    """
    base = f"{api_url.rstrip('/')}/repos/{repo}/actions"
    runs = collect_paginated(
        lambda page: _get_json(runs_listing_url(api_url, repo, now, page), token),
        "workflow_runs",
    )
    jobs: list[dict[str, Any]] = []
    for run in runs:
        jobs.extend(collect_paginated(_run_jobs_page(base, run["id"], token), "jobs"))
    return jobs


def _run_jobs_page(base: str, run_id: int, token: str) -> Callable[[int], dict[str, Any]]:
    """The page reader for one run's jobs, bound to that run."""

    def fetch(page: int) -> dict[str, Any]:
        return _get_json(
            f"{base}/runs/{run_id}/jobs?filter=all&per_page={PER_PAGE}&page={page}", token
        )

    return fetch


# ----- CLI --------------------------------------------------------------------


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        raise BudgetGateError(f"environment variable {name} is not set")
    return value


def _write_summary(decision: GateDecision, month: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    rows = [
        f"| {b.vendor} | {b.used_minutes:g} | {b.limit_minutes:g} | {b.free_minutes} |"
        for b in decision.budgets
    ]
    text = "\n".join(
        [
            f"### Runner canary budget gate ({month}, UTC)",
            "",
            "| vendor | used (basis min) | refuse at | free tier |",
            "|:--|--:|--:|--:|",
            *rows,
            "",
        ]
    )
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    try:
        config = load_config(args.config)
        owner = _required_env("GITHUB_REPOSITORY_OWNER")
        repo = _required_env("GITHUB_REPOSITORY")
        enabled_raw = _required_env("CANARY_VENDORS_ENABLED")
        requested = _required_env("CANARY_REQUESTED_VENDOR")
        # Placement and enablement are decided before any network read, so a misrouted or
        # disabled vendor is refused by name even when the billing API is down.
        vendors = select_vendors(
            owner=owner,
            repository=repo,
            enabled_raw=enabled_raw,
            requested=requested,
            config=config,
        )
        token = _required_env("GH_TOKEN")
        api_url = _required_env("GITHUB_API_URL")
        output_path = _required_env("GITHUB_OUTPUT")
    except BudgetGateError as exc:
        print(f"::error::budget gate: {exc}")
        return 1
    try:
        # Each vendor's own target repository. select_vendors has already refused any vendor
        # targeting another repository, so this is the one repository github.token can read.
        jobs_by_repo = {
            target: fetch_month_jobs(api_url, target, token, now)
            for target in sorted({config.vendors[v].target_repo for v in vendors})
        }
        decision = gate_decision(
            owner=owner,
            repository=repo,
            enabled_raw=enabled_raw,
            requested=requested,
            jobs_by_repo=jobs_by_repo,
            config=config,
            now=now,
        )
    except BillingUnreadable as exc:
        print(
            "::error::budget gate UNKNOWN: billed minutes could not be read "
            f"({exc}); canary refused"
        )
        return 2
    except BudgetGateError as exc:
        print(f"::error::budget gate: {exc}")
        return 1
    for budget in decision.budgets:
        print(
            f"[budget] {budget.vendor}: {budget.used_minutes:g} of {budget.free_minutes} "
            f"free minutes used in {now:%Y-%m}; refuse at {budget.limit_minutes:g}"
        )
    print(decision.planned_vendors_notice())
    with open(output_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(decision.github_output_lines()) + "\n")
    _write_summary(decision, f"{now:%Y-%m}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
