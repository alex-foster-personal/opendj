"""Runner-canary budget gate: refuse a canary run past 80% of a vendor's free tier.

The first job of `.github/workflows/runner-canary.yml`. It sums this UTC calendar month's
billed minutes per vendor runner label from the GitHub Actions jobs API, each job rounded UP
to a whole minute and scaled from the label's vCPU count to the vCPU basis the vendor's free
tier is quoted in, and fails red with an explicit `::error::` at or past
`budget_gate_fraction` (0.8) of that tier. It also decides which vendors run at all: only
those named in `vars.CANARY_VENDORS_ENABLED`, and never a fallback runner for the rest.

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
- [if] the billing read errors, is paginated short, or exceeds the listing cap [then] exit 2
  UNKNOWN with no outputs, [else stop] ✔︎ ✅ 🎯
- [if] a vendor is not in the enable list [then] it is absent from the outputs, and
  dispatching it by name is exit 1, [else stop] ✔︎ ✅ 🎯
- [if] the enable list is empty, unset, malformed or names an unknown vendor [then] exit 1,
  [else stop] ✔︎ ✅ 🎯
- [if] the repository owner is not the configured mirror owner [then] exit 1, [else stop]
  ✔︎ ✅ 🎯
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: GitHub's list endpoints stop returning rows past 1,000 results for a filtered query.
LISTING_CAP = 1000
PER_PAGE = 100
HTTP_TIMEOUT_S = 30


class BudgetGateError(Exception):
    """A refusal the gate can name: over budget, disabled vendor, bad config. Exit 1."""


class BillingUnreadable(Exception):
    """The billing read could not be completed. UNKNOWN, exit 2, never green."""


# ----- config -----------------------------------------------------------------


@dataclass(frozen=True)
class Vendor:
    name: str
    label: str
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


def load_config(path: Path) -> CanaryConfig:
    raw = json.loads(Path(path).read_text())
    vendors = {
        name: Vendor(
            name=name,
            label=v["label"],
            vcpu=int(v["vcpu"]),
            free_minutes_per_month=int(v["free_minutes_per_month"]),
            free_minutes_vcpu_basis=int(v["free_minutes_vcpu_basis"]),
            sources=tuple(v["sources"]),
        )
        for name, v in raw["vendors"].items()
    }
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


def billed_basis_minutes(job: dict[str, Any], vendor: Vendor, now: datetime) -> float:
    """One job's minutes against the free tier: whole minutes rounded up, vCPU-scaled."""
    started = job.get("started_at")
    # A skipped job never had a runner. GitHub still stamps it, and stamps it BACKWARDS
    # (completed_at one second before started_at, measured on the source repository's
    # jobs API, Tue 29 Sep 2026: 13 of 13 skipped jobs in a 3-minute window).
    if not started or job.get("conclusion") == "skipped":
        return 0
    start = _parse_ts(started)
    completed = job.get("completed_at")
    end = _parse_ts(completed) if completed else now
    seconds = (end - start).total_seconds()
    if seconds < 0:
        raise BillingUnreadable(f"job ends before it starts: {started} -> {completed}")
    minutes = max(1, math.ceil(seconds / 60))
    return minutes * vendor.vcpu / vendor.free_minutes_vcpu_basis


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
    vendors: Iterable[str], jobs: list[dict[str, Any]], config: CanaryConfig, now: datetime
) -> list[VendorBudget]:
    budgets = []
    for name in vendors:
        vendor = config.vendors[name]
        used = sum(
            billed_basis_minutes(job, vendor, now)
            for job in jobs
            if vendor.label in (job.get("labels") or [])
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


def check_owner(owner: str, config: CanaryConfig) -> None:
    if owner != config.mirror_owner:
        raise BudgetGateError(
            f"repository owner {owner!r} is not the canary mirror owner "
            f"{config.mirror_owner!r}; the canary runs only in the mirror"
        )


def gate_decision(
    *,
    owner: str,
    enabled_raw: str,
    requested: str,
    jobs: list[dict[str, Any]],
    config: CanaryConfig,
    now: datetime,
) -> GateDecision:
    check_owner(owner, config)
    vendors = requested_vendors(requested, parse_enabled_vendors(enabled_raw, config))
    budgets = evaluate_budgets(vendors, jobs, config, now)
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


def fetch_month_jobs(api_url: str, repo: str, token: str, since: datetime) -> list[dict[str, Any]]:
    """Every job (all attempts) of every run in `repo` created on or after `since`."""
    base = f"{api_url.rstrip('/')}/repos/{repo}/actions"
    created = since.strftime("%Y-%m-%d")
    runs = collect_paginated(
        lambda page: _get_json(
            f"{base}/runs?created=%3E%3D{created}&per_page={PER_PAGE}&page={page}", token
        ),
        "workflow_runs",
    )
    jobs: list[dict[str, Any]] = []
    for run in runs:
        jobs.extend(
            collect_paginated(
                lambda page, run_id=run["id"]: _get_json(
                    f"{base}/runs/{run_id}/jobs?filter=all&per_page={PER_PAGE}&page={page}", token
                ),
                "jobs",
            )
        )
    return jobs


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
        check_owner(owner, config)
        enabled_raw = _required_env("CANARY_VENDORS_ENABLED")
        requested = _required_env("CANARY_REQUESTED_VENDOR")
        # Enablement is decided before any network read, so a disabled vendor is refused
        # by name even when the billing API is down.
        requested_vendors(requested, parse_enabled_vendors(enabled_raw, config))
        token = _required_env("GH_TOKEN")
        repo = _required_env("GITHUB_REPOSITORY")
        api_url = _required_env("GITHUB_API_URL")
        output_path = _required_env("GITHUB_OUTPUT")
    except BudgetGateError as exc:
        print(f"::error::budget gate: {exc}")
        return 1
    try:
        jobs = fetch_month_jobs(api_url, repo, token, month_start(now))
        decision = gate_decision(
            owner=owner,
            enabled_raw=enabled_raw,
            requested=requested,
            jobs=jobs,
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
    with open(output_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(decision.github_output_lines()) + "\n")
    _write_summary(decision, f"{now:%Y-%m}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
