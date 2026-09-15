#!/usr/bin/env python3
"""Roll every recent Actions run into one spend ledger and gate on the allowance.

`ci_cost_guard.py` prices ONE run and alerts when that single run is expensive.
Individually inexpensive runs can still exhaust an account's cumulative
allowance. The ledger tracks that aggregate rather than relying on one run.

The model lives in ci_cost_model.py and the Markdown rendering in
ci_cost_report.py; this module owns the GitHub API, the cache, and the CLI.

Exits non-zero when month-to-date allowance consumption crosses `--stop-pct`,
so a scheduled workflow can fail loudly while there is still headroom left.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

try:
    from scripts.ci_cost_model import (
        ALLOWANCE_MULTIPLIERS,
        PLAN_ALLOWANCE_MINUTES,
        Coverage,
        RunRow,
        allowance_multiplier,
        price_run,
    )
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.ci_cost_ledger") from None
    raise
from scripts.ci_cost_billing import (
    MAX_BILLING_API_CALLS,
    build_billing_ledger,
    fetch_billing_usage,
)
from scripts.ci_cost_report import (
    group,
    month_to_date,
    render_billing_report,
    render_report,
)

__all__ = [
    "ALLOWANCE_MULTIPLIERS",
    "PLAN_ALLOWANCE_MINUTES",
    "Coverage",
    "RunRow",
    "allowance_multiplier",
    "build_ledger",
    "CacheLoad",
    "fetch_jobs",
    "fetch_runs",
    "group",
    "inspect_cache",
    "load_cache",
    "merge_report_rows",
    "month_to_date",
    "price_run",
    "render_report",
    "save_cache",
]

# ----- GitHub API ---------------------------------------------------------


def _get(url: str, token: str) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "music-dj-tools-ci-cost-ledger",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code in (403, 429):
            # Secondary rate limits return 403 with no Retry-After. Say so
            # plainly: the bare traceback reads like an auth failure and sends
            # the next reader off rotating a perfectly good token.
            remaining = error.headers.get("x-ratelimit-remaining", "?")
            reset = error.headers.get("x-ratelimit-reset", "?")
            raise SystemExit(
                f"GitHub API refused the request ({error.code}). "
                f"core remaining={remaining}, reset={reset}. "
                "This is almost always a secondary rate limit from too many "
                "recent calls, not a bad token -- wait and re-run."
            ) from error
        raise


def fetch_runs(
    repository: str, since: str, token: str, max_pages: int
) -> tuple[list[dict[str, Any]], int]:
    """List runs created on or after `since`, with the server's own total.

    Returns (runs, total_count). The two differ, and the difference is the
    whole point: adding a `created=` filter makes this endpoint search-backed,
    and **GitHub then caps pagination at 1,000 results** while still reporting
    the true `total_count`; an exhausted page does not establish that total.

    The first version of this function inferred truncation from
    `len(runs) >= max_pages * 100`, which is the wrong bound entirely: at the
    1,000-row wall the pages simply run out, so that check reported
    `truncated: false` while the ledger silently priced only part of the
    month and presented it as the total. Trust the server's count, never the page walk.
    """
    runs: list[dict[str, Any]] = []
    total = 0
    for page in range(1, max_pages + 1):
        url = (
            f"https://api.github.com/repos/{repository}/actions/runs"
            f"?per_page=100&page={page}&created=%3E%3D{since}"
        )
        payload = _get(url, token)
        # Keep the largest count seen, never the latest. The page past the
        # 1,000-row wall returns `total_count: 0` alongside an empty list, so
        # assigning the latest value clobbers the real total with zero on the
        # final iteration.
        total = max(total, int(payload.get("total_count") or 0))
        batch = payload.get("workflow_runs") or []
        runs.extend(batch)
        if len(batch) < 100:
            break
    return runs, total


def fetch_jobs(repository: str, run_id: int, token: str) -> list[dict[str, Any]]:
    url = f"https://api.github.com/repos/{repository}/actions/runs/{run_id}/jobs?per_page=100"
    try:
        return _get(url, token).get("jobs") or []
    except HTTPError as error:
        if error.code == 404:
            # Logs and job records age out before run records do.
            return []
        raise


# ----- CLI ----------------------------------------------------------------


def _write_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT", "")
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


@dataclass(frozen=True)
class CacheLoad:
    rows: dict[int, RunRow]
    gap: str | None


def inspect_cache(path: Path | None) -> CacheLoad:
    if path is None:
        return CacheLoad(rows={}, gap=None)
    if not path.exists():
        print(f"[ledger] WARN: cache {path} missing; treating as empty")
        return CacheLoad(rows={}, gap="missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError(f"expected object, got {type(payload).__name__}")
        rows_payload = payload.get("rows")
        if rows_payload is None:
            raise KeyError("rows")
        if not isinstance(rows_payload, list):
            raise TypeError(f"rows must be a list, got {type(rows_payload).__name__}")
        rows = {int(row["run_id"]): RunRow.from_json(row) for row in rows_payload}
    except (OSError, json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError) as error:
        print(f"[ledger] WARN: cache {path} unreadable ({error}); treating as empty")
        return CacheLoad(rows={}, gap="unreadable")
    return CacheLoad(rows=rows, gap=None)


def load_cache(path: Path | None) -> dict[int, RunRow]:
    return inspect_cache(path).rows


def _refuse_empty_past_month(month: str, rows: list[RunRow]) -> None:
    current_month = datetime.now(UTC).strftime("%Y-%m")
    if month != current_month and not month_to_date(rows, month):
        raise SystemExit(
            f"No priced runs for {month} in cache or the API listing. "
            f"Dispatch during {month} or pass --runs-json."
        )


def merge_report_rows(
    walked_rows: list[RunRow],
    cache: dict[int, RunRow],
    month: str,
) -> list[RunRow]:
    """Add cached rows for `month` that this listing walk never reached.

    GitHub returns newest-first and caps pagination, so a warm cache often
    holds priced runs that have already fallen off the listing. Dropping them
    on save made every run report `0 from cache` and a moving floor.
    """
    by_id = {row.run_id: row for row in walked_rows}
    for run_id, cached in cache.items():
        if cached.is_terminal and cached.created_at.startswith(month) and run_id not in by_id:
            by_id[run_id] = cached
    return list(by_id.values())


def save_cache(
    path: Path | None,
    rows: Iterable[RunRow],
    keep_months: set[str],
    prior: dict[int, RunRow] | None = None,
) -> None:
    if not path:
        return
    kept_by_id: dict[int, RunRow] = {
        run_id: row
        for run_id, row in (prior or {}).items()
        if row.is_terminal and row.created_at[:7] in keep_months
    }
    for row in rows:
        if row.is_terminal and row.created_at[:7] in keep_months:
            kept_by_id[row.run_id] = row
    kept = [row.to_json() for row in kept_by_id.values()]
    path.write_text(json.dumps({"rows": kept}), encoding="utf-8")


def build_ledger(
    repository: str,
    since: str,
    token: str,
    max_pages: int,
    cache: dict[int, RunRow] | None = None,
    max_api_calls: int = 400,
) -> tuple[list[RunRow], Coverage]:
    """Price every run since `since`, reusing cached rows for finished runs.

    A full month-to-date pass costs one API call per run. GITHUB_TOKEN is
    capped at 1,000 requests/hour/repository, and this repository saw 1,610
    runs in August 2026 -- so an uncached pass would start failing in exactly
    the busy month the watcher exists for. Finished runs never change, so
    only new and still-running ones are fetched.
    """
    cache = cache or {}
    runs, total_count = fetch_runs(repository, since, token, max_pages)

    rows: list[RunRow] = []
    fetched = 0
    budget_hit = False
    for run in runs:
        run_id = int(run["id"])
        cached = cache.get(run_id)
        if cached is not None and cached.is_terminal:
            rows.append(cached)
            continue
        if fetched >= max_api_calls:
            # Budget exhausted: stop PRICING, but keep WALKING. The listing is
            # newest-first, so on a busy day the budget goes entirely on new
            # runs and every cached older row sits further down the list. The
            # first version used `break` here, which threw those cached rows
            # away before the loop ever reached them -- so the cache saved only
            # each run's fresh 400 rows, never accumulated, and every scheduled
            # run reported '0 from cache' at 15% coverage while `actions/cache`
            # itself restored and saved perfectly (observed 2 Sep 2026,
            # three consecutive scheduled runs). `continue` keeps the walk
            # alive so cached rows still land in `rows` and get re-saved.
            budget_hit = True
            continue
        rows.append(price_run(run, fetch_jobs(repository, run_id, token)))
        fetched += 1

    coverage = Coverage(
        priced=len(rows),
        listed=len(runs),
        server_total=total_count,
        api_calls=fetched,
        api_budget_hit=budget_hit,
    )
    return rows, coverage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--token", default=os.environ.get("GH_TOKEN"))
    parser.add_argument("--since", help="YYYY-MM-DD; defaults to the 1st of --month")
    parser.add_argument("--month", help="YYYY-MM; defaults to the current UTC month")
    parser.add_argument("--plan", choices=sorted(PLAN_ALLOWANCE_MINUTES), default="pro")
    parser.add_argument(
        "--allowance-minutes",
        type=int,
        help="override the plan's included minutes",
    )
    parser.add_argument("--warn-pct", type=float, default=70.0)
    parser.add_argument("--stop-pct", type=float, default=90.0)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument(
        "--max-api-calls",
        type=int,
        default=400,
        help="ceiling on per-run job fetches; GITHUB_TOKEN allows 1,000/hour/repo",
    )
    parser.add_argument("--report-file", type=Path, default=Path("ci-cost-ledger.md"))
    parser.add_argument("--json-file", type=Path)
    parser.add_argument(
        "--runs-json",
        type=Path,
        help="offline per-run fixture: {runs: [...], jobs: {run_id: [...]}}",
    )
    parser.add_argument(
        "--billing-json",
        type=Path,
        help="offline billing fixture: {usageItems: [...]}",
    )
    parser.add_argument(
        "--billing-user",
        help="GitHub user for billing/usage (default: owner of --repository)",
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        help="JSON cache of already-priced finished runs; read and rewritten",
    )
    parser.add_argument(
        "--fail-on-stop",
        action="store_true",
        help="exit 1 when month-to-date crosses --stop-pct",
    )
    args = parser.parse_args()

    month = args.month or datetime.now(UTC).strftime("%Y-%m")
    since = args.since or f"{month}-01"
    allowance = args.allowance_minutes or PLAN_ALLOWANCE_MINUTES[args.plan]

    billing_ledger = None
    rows: list[RunRow] = []
    coverage: Coverage | None = None
    repository = args.repository or "offline/fixture"

    if args.billing_json:
        fixture = json.loads(args.billing_json.read_text(encoding="utf-8"))
        items = fixture.get("usageItems") or []
        billing_ledger = build_billing_ledger(
            items,
            repository=repository,
            month=month,
            api_calls=0,
            now=datetime.fromisoformat(fixture["now"]) if fixture.get("now") else None,
        )
    elif args.runs_json:
        fixture = json.loads(args.runs_json.read_text(encoding="utf-8"))
        jobs_by_run = {str(k): v for k, v in fixture.get("jobs", {}).items()}
        rows = [price_run(run, jobs_by_run.get(str(run["id"]), [])) for run in fixture["runs"]]
        coverage = Coverage(
            priced=len(rows),
            listed=len(rows),
            server_total=len(rows),
            api_calls=0,
            api_budget_hit=False,
        )
    else:
        if not args.repository or not args.token:
            parser.error(
                "--repository and --token (or GH_TOKEN) are required without "
                "--runs-json or --billing-json"
            )
        repository = args.repository
        billing_user = args.billing_user or repository.split("/", 1)[0]
        year = int(month[:4])
        month_num = int(month[5:])
        items, api_calls = fetch_billing_usage(billing_user, year, month_num, args.token)
        if api_calls > MAX_BILLING_API_CALLS:
            raise SystemExit(
                f"billing ledger made {api_calls} API calls; limit is {MAX_BILLING_API_CALLS}"
            )
        billing_ledger = build_billing_ledger(
            items,
            repository=repository,
            month=month,
            api_calls=api_calls,
        )
        print(
            f"[ledger] billing usage for {repository}: {api_calls} API call(s), "
            f"measured through {billing_ledger.measured_at or 'n/a'}"
            + (
                ""
                if billing_ledger.is_trusted and not billing_ledger.unknown_reason
                else f"  <-- {billing_ledger.unknown_reason or 'HOSTED VIOLATION'}"
            )
        )

    if billing_ledger is not None:
        report, summary = render_billing_report(
            billing_ledger,
            allowance_limit=allowance,
            warn_pct=args.warn_pct,
            stop_pct=args.stop_pct,
        )
    else:
        _refuse_empty_past_month(month, rows)
        report, summary = render_report(
            rows,
            repository=repository,
            month=month,
            allowance_limit=allowance,
            warn_pct=args.warn_pct,
            stop_pct=args.stop_pct,
            coverage=coverage or Coverage(0, 0, 0, 0, False),
        )

    args.report_file.write_text(report, encoding="utf-8")
    if args.json_file:
        args.json_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(report)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as handle:
            handle.write(report)

    for key, value in summary.items():
        _write_output(key, str(value))
    _write_output("report_file", str(args.report_file))

    if summary["state"] == "HOSTED":
        violation = summary["hosted_violations"][0]
        print(
            f"::error title=Hosted Actions billing after cutover::"
            f"{violation['day']} {violation['sku']} net ${violation['net_usd']:.2f}"
        )
        return 1
    if args.fail_on_stop and summary["state"] == "STOP":
        print(
            f"::error title=CI allowance {summary['allowance_pct']:.0f}% consumed::"
            f"{summary['allowance_used']:.0f}/{allowance} minutes used in {month}."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
