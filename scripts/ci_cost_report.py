#!/usr/bin/env python3
"""Render a priced ledger as Markdown, with coverage stated up front.

Split out of ci_cost_ledger.py when that file crossed the repository's
600-line ceiling. The dependency runs one way only:
ci_cost_model -> ci_cost_report -> ci_cost_ledger.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from scripts.ci_cost_billing import BillingLedger
from scripts.ci_cost_model import Coverage, RunRow


def _sum(rows: Iterable[RunRow], key: str) -> float:
    return sum(getattr(row, key) for row in rows)


def group(rows: list[RunRow], attr: str) -> list[tuple[str, list[RunRow]]]:
    buckets: dict[str, list[RunRow]] = defaultdict(list)
    for row in rows:
        buckets[getattr(row, attr)].append(row)
    return sorted(
        buckets.items(),
        key=lambda item: _sum(item[1], "allowance_minutes"),
        reverse=True,
    )


def month_to_date(rows: list[RunRow], month: str) -> list[RunRow]:
    return [row for row in rows if row.created_at.startswith(month)]


def _table(header: list[str], aligns: list[str], body: list[list[str]]) -> list[str]:
    return [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(aligns) + " |",
        *["| " + " | ".join(cells) + " |" for cells in body],
    ]


def _bucket_rows(buckets: list[tuple[str, list[RunRow]]], limit: int) -> list[list[str]]:
    body = []
    for label, rows in buckets[:limit]:
        allowance = _sum(rows, "allowance_minutes")
        wasted = sum(r.allowance_minutes for r in rows if r.is_waste)
        median = sorted(r.wall_seconds for r in rows)[len(rows) // 2] / 60
        body.append(
            [
                label,
                str(len(rows)),
                f"{allowance:.0f}",
                f"${_sum(rows, 'cost_usd'):.2f}",
                f"{median:.1f}",
                f"{wasted:.0f}",
            ]
        )
    return body


def render_report(
    rows: list[RunRow],
    *,
    repository: str,
    month: str,
    allowance_limit: int,
    warn_pct: float,
    stop_pct: float,
    coverage: Coverage,
) -> tuple[str, dict[str, Any]]:
    mtd = month_to_date(rows, month)
    used = _sum(mtd, "allowance_minutes")
    pct = (used / allowance_limit * 100) if allowance_limit else 0.0
    if not coverage.is_complete:
        state = "UNKNOWN"
    elif pct >= stop_pct:
        state = "STOP"
    elif pct >= warn_pct:
        state = "WARN"
    else:
        state = "OK"
    wasted = sum(r.allowance_minutes for r in mtd if r.is_waste)
    unpriced = [r for r in rows if r.unpriced_jobs]

    floor = "" if coverage.is_complete else " (FLOOR, see coverage)"
    lines = [
        f"# CI spend ledger -- {repository}",
        "",
        f"- Month: **{month}**",
        f"- Coverage: **{coverage.priced} of {coverage.server_total} runs priced "
        f"({coverage.pct:.0f}%)**",
        f"- Allowance used: **{used:.0f} / {allowance_limit} min "
        f"({pct:.1f}%)**{floor} -> **{state}**",
        f"- Gross cash at overage rates: **${_sum(mtd, 'cost_usd'):.2f}**",
        f"- Burned on cancelled/refused runs: **{wasted:.0f} min** "
        f"({(wasted / used * 100) if used else 0:.1f}% of spend)",
        "",
        "Allowance minutes are wall minutes multiplied per platform "
        "(Linux 1x, Windows 2x, macOS 10x); that multiplied number, not the "
        "cash figure, is what stops CI when it runs out.",
        "",
    ]

    if not coverage.is_complete:
        missed = coverage.server_total - coverage.priced
        if coverage.cache_gap:
            if missed > 0:
                why = (
                    f"the priced-run cache was {coverage.cache_gap}, and "
                    f"{missed} of {coverage.server_total} runs were not priced"
                )
            else:
                why = f"the priced-run cache was {coverage.cache_gap}"
        elif coverage.api_budget_hit:
            why = "the --max-api-calls budget was reached"
        else:
            why = "GitHub caps a `created=`-filtered listing at 1,000 rows"
        lines += [
            f"> **INCOMPLETE: {missed} of {coverage.server_total} runs were not "
            f"priced** because {why}. Every number below is a **floor**, not a "
            "total, and the real spend is higher. Do not quote these figures "
            "without this sentence.",
            "",
        ]

    header = ["", "Runs", "Allowance min", "Cost", "Median wall min", "Wasted min"]
    aligns = ["---", "---:", "---:", "---:", "---:", "---:"]

    for title, attr, limit in (
        ("Per day", "day", 31),
        ("Per workflow", "workflow", 15),
        ("Per branch (top 15)", "branch", 15),
        ("Per event", "event", 10),
    ):
        lines += [f"## {title}", ""]
        body = _bucket_rows(group(rows, attr), limit)
        if attr == "day":
            body.sort(key=lambda cells: cells[0])
        lines += [*_table([title.split(" (")[0], *header[1:]], aligns, body), ""]

    priciest = sorted(rows, key=lambda r: r.allowance_minutes, reverse=True)[:10]
    lines += ["## Most expensive individual runs", ""]
    lines += _table(
        ["Run", "Workflow", "Branch", "Result", "Allowance min", "Wall min"],
        ["---", "---", "---", "---", "---:", "---:"],
        [
            [
                f"[{r.run_id}](https://github.com/{repository}/actions/runs/{r.run_id})",
                r.workflow,
                r.branch,
                r.conclusion,
                f"{r.allowance_minutes}",
                f"{r.wall_seconds / 60:.1f}",
            ]
            for r in priciest
        ],
    )
    lines.append("")

    if unpriced:
        lines += ["## Unpriced jobs (telemetry failure -- fix before trusting totals)", ""]
        lines += [f"- run {r.run_id}: {', '.join(r.unpriced_jobs)}" for r in unpriced[:20]]
        lines.append("")

    summary = {
        "state": state,
        "month": month,
        "allowance_used": used,
        "allowance_limit": allowance_limit,
        "allowance_pct": pct,
        "cost_usd": _sum(mtd, "cost_usd"),
        "wasted_minutes": wasted,
        "run_count": len(rows),
        "runs_priced": coverage.priced,
        "runs_total": coverage.server_total,
        "coverage_pct": round(coverage.pct, 1),
        "complete": coverage.is_complete,
        "api_calls": coverage.api_calls,
        "cache_gap": coverage.cache_gap,
        "unpriced_runs": len(unpriced),
    }
    return "\n".join(lines), summary


def render_billing_report(
    ledger: BillingLedger,
    *,
    allowance_limit: int,
    warn_pct: float,
    stop_pct: float,
) -> tuple[str, dict[str, Any]]:
    """Render a billing-API ledger. Fails closed when data is not trusted."""
    if ledger.unknown_reason:
        state = "UNKNOWN"
        used = 0.0
        pct = 0.0
    elif ledger.hosted_violations:
        state = "HOSTED"
        used = float(ledger.allowance_minutes)
        pct = (used / allowance_limit * 100) if allowance_limit else 0.0
    else:
        used = float(ledger.allowance_minutes)
        pct = (used / allowance_limit * 100) if allowance_limit else 0.0
        if pct >= stop_pct:
            state = "STOP"
        elif pct >= warn_pct:
            state = "WARN"
        else:
            state = "OK"

    measured = ledger.measured_at or "n/a"
    lines = [
        f"# CI spend ledger -- {ledger.repository}",
        "",
        f"- Month: **{ledger.month}**",
        f"- Source: **GitHub billing usage API** ({ledger.api_calls} API call(s))",
        f"- Measured through: **{measured}**",
    ]

    if ledger.unknown_reason:
        lines += [
            f"- Verdict: **{state}** ({ledger.unknown_reason})",
            "",
            "> **UNTRUSTED:** No allowance figure is quoted below because the "
            "billing feed is incomplete or stale.",
            "",
        ]
    elif ledger.hosted_violations:
        lines += [
            f"- Allowance used: **{used:.0f} / {allowance_limit} min ({pct:.1f}%)** "
            f"-> **{state}**",
            f"- Net billed (hosted SKUs): **${ledger.net_usd:.2f}**",
            "",
            "> **HOSTED BILLING AFTER CUTOVER:** GitHub-hosted Actions minutes "
            f"with net spend appeared after {ledger.hosted_violations[0].day}. "
            "Self-hosted cutover was Wed 3 Sep 2026.",
            "",
        ]
        for violation in ledger.hosted_violations:
            lines.append(
                f"- **{violation.day}** `{violation.sku}` net **${violation.net_usd:.2f}**"
            )
        lines.append("")
    else:
        lines += [
            f"- Allowance used: **{used:.0f} / {allowance_limit} min ({pct:.1f}%)** "
            f"-> **{state}**",
            f"- Net billed (this repo): **${ledger.net_usd:.2f}**",
            "",
            "Allowance minutes apply Linux 1x, Windows 2x, macOS 10x to the "
            "billing quantity for each Actions SKU.",
            "",
        ]

    if ledger.skus and not ledger.unknown_reason:
        lines += ["## Per SKU (billing usage)", ""]
        lines += _table(
            ["SKU", "Minutes", "Allowance min", "Net $"],
            ["---", "---:", "---:", "---:"],
            [
                [
                    row.sku,
                    str(row.minutes),
                    str(row.allowance_minutes),
                    f"${row.net_usd:.2f}",
                ]
                for row in ledger.skus
            ],
        )
        lines.append("")

    summary = {
        "state": state,
        "month": ledger.month,
        "allowance_used": used if not ledger.unknown_reason else 0.0,
        "allowance_limit": allowance_limit,
        "allowance_pct": pct if not ledger.unknown_reason else 0.0,
        "cost_usd": ledger.net_usd if not ledger.unknown_reason else 0.0,
        "measured_at": ledger.measured_at,
        "complete": ledger.is_trusted and not ledger.unknown_reason,
        "api_calls": ledger.api_calls,
        "unknown_reason": ledger.unknown_reason,
        "hosted_violations": [
            {"day": v.day, "sku": v.sku, "net_usd": v.net_usd} for v in ledger.hosted_violations
        ],
        "source": "billing_usage",
    }
    return "\n".join(lines), summary
