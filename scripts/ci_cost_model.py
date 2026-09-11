#!/usr/bin/env python3
"""Roll every recent Actions run into one spend ledger and gate on the allowance.

`ci_cost_guard.py` prices ONE run and alerts when that single run is expensive.
Individually inexpensive runs can still exhaust an account's cumulative
allowance. The ledger tracks that aggregate rather than relying on one run.

This module supplies that missing view. It reports two different units, because
conflating them is what produced the wrong post-incident estimate:

- **allowance minutes** -- what the monthly included-minutes bucket is drained
  by. Windows costs 2x and macOS 10x the wall time. This is the number that
  decides whether CI stops working, so it is what the budget gate reads.
- **USD** -- the gross cash rate once the allowance is gone, taken from
  `ci_cost_guard.STANDARD_SKUS` so both tools can never disagree about price.

Exits non-zero when month-to-date allowance consumption crosses `--stop-pct`,
so a scheduled workflow can fail loudly while there is still headroom left.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from scripts.ci_cost_guard import elapsed_seconds, infer_standard_sku

# GitHub drains the included-minutes bucket at a per-platform multiplier that is
# unrelated to the cash rate. Documented at
# https://docs.github.com/en/billing/concepts/product-billing/github-actions
ALLOWANCE_MULTIPLIERS = {"linux": 1, "windows": 2, "macos": 10}

# Included minutes per month by plan, same source. Passed in explicitly rather
# than inferred: reading the plan needs a token scope this tool deliberately
# does not require.
PLAN_ALLOWANCE_MINUTES = {"free": 2000, "pro": 3000, "team": 3000}


def allowance_multiplier(labels: Iterable[str]) -> int:
    """Return the included-minutes multiplier for a job's runner labels.

    Fails closed on an unrecognized label: charging an unknown runner at the
    cheapest rate is exactly how a runaway stays invisible.
    """
    normalized = {label.lower() for label in labels}
    if "self-hosted" in normalized:
        return 0
    for label in normalized:
        if label.startswith("macos"):
            return ALLOWANCE_MULTIPLIERS["macos"]
        if label.startswith("windows"):
            return ALLOWANCE_MULTIPLIERS["windows"]
        if label.startswith("ubuntu") or label == "linux":
            return ALLOWANCE_MULTIPLIERS["linux"]
    raise ValueError(f"unpriced runner labels, refusing to guess: {sorted(normalized)}")


@dataclass
class RunRow:
    """One workflow run, priced."""

    run_id: int
    workflow: str
    branch: str
    event: str
    conclusion: str
    created_at: str
    wall_seconds: float
    job_count: int
    raw_minutes: float
    allowance_minutes: int
    cost_usd: float
    unpriced_jobs: list[str] = field(default_factory=list)

    @property
    def day(self) -> str:
        return self.created_at[:10]

    @property
    def is_terminal(self) -> bool:
        """True once the run can no longer change, so it is safe to cache."""
        return self.conclusion in {
            "success",
            "failure",
            "cancelled",
            "skipped",
            "timed_out",
            "action_required",
            "neutral",
            "stale",
            "startup_failure",
        }

    def to_json(self) -> dict[str, Any]:
        return dict(vars(self))

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> RunRow:
        return cls(**payload)

    @property
    def is_waste(self) -> bool:
        """Runs that consumed the allowance without producing a green signal."""
        return self.conclusion in {"cancelled", "startup_failure"}


def price_run(run: dict[str, Any], jobs: list[dict[str, Any]]) -> RunRow:
    raw_minutes = 0.0
    allowance_minutes = 0
    cost_usd = 0.0
    counted_jobs = 0
    unpriced: list[str] = []

    for job in jobs:
        seconds = elapsed_seconds(job.get("started_at"), job.get("completed_at"))
        if seconds is None:
            # Never scheduled -- consumes nothing. A job GitHub refused to
            # start (the exhausted-allowance signature) lands here.
            continue
        counted_jobs += 1
        raw_minutes += seconds / 60
        # GitHub rounds every started job up to a whole minute.
        billed = max(1, math.ceil(seconds / 60))
        labels = job.get("labels") or []
        try:
            allowance_minutes += billed * allowance_multiplier(labels)
        except ValueError:
            unpriced.append(job.get("name", "unnamed job"))
            continue
        sku = infer_standard_sku(labels)
        if sku is None:
            unpriced.append(job.get("name", "unnamed job"))
            continue
        cost_usd += billed * sku.rate_usd_per_minute

    started = run.get("run_started_at") or run.get("created_at")
    wall = elapsed_seconds(started, run.get("updated_at")) or 0.0

    return RunRow(
        run_id=int(run["id"]),
        workflow=run.get("name") or "unnamed workflow",
        branch=run.get("head_branch") or "(none)",
        event=run.get("event") or "unknown",
        conclusion=run.get("conclusion") or "in_progress",
        created_at=run["created_at"],
        wall_seconds=wall,
        job_count=counted_jobs,
        raw_minutes=raw_minutes,
        allowance_minutes=allowance_minutes,
        cost_usd=cost_usd,
        unpriced_jobs=unpriced,
    )


@dataclass(frozen=True)
class Coverage:
    """How much of the month the ledger actually saw.

    Every total must be read against this. `server_total` is GitHub's own
    count for the window; `priced` is what was costed. When they differ the
    report is a FLOOR, and says so. A missing or unreadable ``--cache-file``
    is also a FLOOR, even when ``priced == server_total``.
    """

    priced: int
    listed: int
    server_total: int
    api_calls: int
    api_budget_hit: bool
    cache_gap: str | None = None

    @property
    def is_complete(self) -> bool:
        return (
            self.priced >= self.server_total
            and not self.api_budget_hit
            and self.cache_gap is None
        )

    @property
    def pct(self) -> float:
        return (self.priced / self.server_total * 100) if self.server_total else 100.0
