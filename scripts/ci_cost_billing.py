#!/usr/bin/env python3
"""Month-to-date CI spend from GitHub's billing usage API.

Replaces per-run pricing for the allowance ledger: one API call returns every
repo row for the month instead of one call per workflow run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# Hosted billing through the configured cutover is expected; a net-positive
# hosted row after the cutover is a leak.
SELF_HOSTED_CUTOVER = date(2026, 9, 3)

HOSTED_ACTIONS_PREFIXES = ("Actions Linux", "Actions Windows", "Actions macOS")

FRESHNESS_MAX_AGE = timedelta(hours=48)

MAX_BILLING_API_CALLS = 5


def _get(url: str, token: str) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "music-dj-tools-ci-cost-billing",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
            if not isinstance(payload, dict):
                raise TypeError(f"expected object, got {type(payload).__name__}")
            return payload
    except HTTPError as error:
        if error.code in (403, 429):
            remaining = error.headers.get("x-ratelimit-remaining", "?")
            reset = error.headers.get("x-ratelimit-reset", "?")
            raise SystemExit(
                f"GitHub billing API refused the request ({error.code}). "
                f"core remaining={remaining}, reset={reset}. "
                "This endpoint needs a user-scoped token (read:user), not "
                "GITHUB_TOKEN alone."
            ) from error
        raise


def is_hosted_actions_sku(sku: str) -> bool:
    return any(sku.startswith(prefix) for prefix in HOSTED_ACTIONS_PREFIXES)


def allowance_multiplier_for_sku(sku: str) -> int:
    if sku.startswith("Actions macOS"):
        return 10
    if sku.startswith("Actions Windows"):
        return 2
    if sku.startswith("Actions Linux"):
        return 1
    return 0


def repository_matches(row_repo: str | None, repository: str) -> bool:
    if not row_repo:
        return False
    short = repository.split("/", 1)[-1]
    return row_repo == repository or row_repo == short


def parse_usage_date(raw: str) -> date:
    return date.fromisoformat(raw[:10])


@dataclass(frozen=True)
class BillingSkuRow:
    sku: str
    minutes: int
    net_usd: float
    allowance_minutes: int


@dataclass(frozen=True)
class HostedViolation:
    day: str
    sku: str
    net_usd: float


@dataclass
class BillingLedger:
    repository: str
    month: str
    skus: list[BillingSkuRow]
    allowance_minutes: int
    net_usd: float
    measured_at: str | None
    api_calls: int
    unknown_reason: str | None = None
    hosted_violations: list[HostedViolation] = field(default_factory=list)

    @property
    def is_trusted(self) -> bool:
        return self.unknown_reason is None and not self.hosted_violations


def fetch_billing_usage(
    billing_user: str,
    year: int,
    month: int,
    token: str,
) -> tuple[list[dict[str, Any]], int]:
    url = (
        f"https://api.github.com/users/{billing_user}/settings/billing/usage"
        f"?year={year}&month={month}"
    )
    payload = _get(url, token)
    items = payload.get("usageItems")
    if items is None:
        raise SystemExit("billing usage response missing usageItems")
    if not isinstance(items, list):
        raise TypeError(f"usageItems must be a list, got {type(items).__name__}")
    return items, 1


def _global_latest_day(items: list[dict[str, Any]]) -> date | None:
    days = [parse_usage_date(str(row["date"])) for row in items if row.get("date")]
    return max(days) if days else None


def _freshness_reason(items: list[dict[str, Any]], now: datetime) -> str | None:
    latest = _global_latest_day(items)
    if latest is None:
        return "billing usage returned no dated rows"
    age = now.date() - latest
    if age > FRESHNESS_MAX_AGE:
        return (
            f"latest billing row is {latest.isoformat()} "
            f"({age.days}d old, limit {FRESHNESS_MAX_AGE.days * 24}h)"
        )
    return None


def build_billing_ledger(
    items: list[dict[str, Any]],
    *,
    repository: str,
    month: str,
    api_calls: int,
    now: datetime | None = None,
) -> BillingLedger:
    """Aggregate billing rows for `repository` and gate on freshness."""
    now = now or datetime.now(UTC)
    repo_rows = [
        row
        for row in items
        if repository_matches(row.get("repositoryName"), repository)
        and str(row.get("date", "")).startswith(month)
    ]

    freshness = _freshness_reason(items, now)
    if not repo_rows:
        return BillingLedger(
            repository=repository,
            month=month,
            skus=[],
            allowance_minutes=0,
            net_usd=0.0,
            measured_at=None,
            api_calls=api_calls,
            unknown_reason=freshness or f"no billing rows for {repository} in {month}",
        )
    if freshness:
        return BillingLedger(
            repository=repository,
            month=month,
            skus=[],
            allowance_minutes=0,
            net_usd=0.0,
            measured_at=None,
            api_calls=api_calls,
            unknown_reason=freshness,
        )

    by_sku: dict[str, list[dict[str, Any]]] = {}
    for row in repo_rows:
        sku = str(row.get("sku") or "unknown")
        by_sku.setdefault(sku, []).append(row)

    sku_rows: list[BillingSkuRow] = []
    allowance_total = 0
    net_total = 0.0
    violations: list[HostedViolation] = []
    measured_days: list[date] = []

    for sku, rows in sorted(by_sku.items()):
        minutes = int(sum(int(row.get("quantity") or 0) for row in rows))
        net_usd = float(sum(float(row.get("netAmount") or 0) for row in rows))
        multiplier = allowance_multiplier_for_sku(sku)
        allowance = minutes * multiplier if multiplier else 0
        sku_rows.append(
            BillingSkuRow(
                sku=sku,
                minutes=minutes,
                net_usd=net_usd,
                allowance_minutes=allowance,
            )
        )
        allowance_total += allowance
        net_total += net_usd
        for row in rows:
            day = parse_usage_date(str(row["date"]))
            measured_days.append(day)
            if (
                is_hosted_actions_sku(sku)
                and float(row.get("netAmount") or 0) > 0
                and day > SELF_HOSTED_CUTOVER
            ):
                violations.append(
                    HostedViolation(day=day.isoformat(), sku=sku, net_usd=float(row["netAmount"]))
                )

    measured_at = max(measured_days).isoformat() if measured_days else None

    return BillingLedger(
        repository=repository,
        month=month,
        skus=sku_rows,
        allowance_minutes=allowance_total,
        net_usd=net_total,
        measured_at=measured_at,
        api_calls=api_calls,
        unknown_reason=None,
        hosted_violations=violations,
    )
