#!/usr/bin/env python3
"""One scheduled pass posts failed CI/E2E/macOS Packaging runs to the error sink.

Moved from the retired CI Cost Guard workflow into CI Budget Watch (ADR-0121).
Lists completions since the previous pass's mark via scripts/ci_run_batch.py and
selects failures with the same helpers as before (RUN-COUNT round 3).
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.ci_cost_guard import (
    reruns_no_pass_listed,
    sink_failure_records,
)
from scripts.ci_run_batch import (
    batch_since,
    created_floor,
    fetch_attempt,
    fetch_completed_runs,
    iso,
    reconcile_created_since,
    reconcile_listing,
)


def _names(csv: str) -> set[str]:
    return {name.strip() for name in csv.split(",") if name.strip()}


def _write_output(path: str, name: str, value: str) -> None:
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def run_sink_batch(args: argparse.Namespace) -> int:
    now = datetime.now(tz=UTC)
    floor = timedelta(minutes=args.overlap_minutes)
    since = batch_since(args.previous_started or None, now, floor)
    created_since = created_floor(since, timedelta(hours=args.lookback_hours))
    listed = _names(args.listed)
    reconciled = (
        reconcile_listing(
            args.repository,
            reconcile_created_since(now, timedelta(days=args.reconcile_horizon_days)),
            created_since,
            args.token,
            workflow_names=listed,
        )
        if args.reconcile_horizon_days is not None
        else []
    )
    runs = fetch_completed_runs(
        args.repository, created_since, args.token, "ci-error-sink-batch", workflow_names=listed
    )
    priced_since = iso(now - timedelta(days=args.reconcile_price_days or 0))

    def read_attempt(run_id: Any, attempt: int) -> dict[str, Any]:
        return fetch_attempt(args.repository, str(run_id), attempt, args.token, "ci-error-sink-batch")

    sink_failures = sink_failure_records(
        (
            (runs, since),
            (reruns_no_pass_listed(reconciled, timedelta(hours=args.lookback_hours)), priced_since),
        ),
        listed,
        read_attempt,
        args.repository,
    )
    sink_file = args.report_dir / "ci-sink-failures.json"
    sink_file.write_text(json.dumps(sink_failures, indent=2), encoding="utf-8")
    output_file = os.environ.get("GITHUB_OUTPUT", "")
    _write_output(output_file, "since", since)
    _write_output(output_file, "sink_failures", str(len(sink_failures)))
    _write_output(output_file, "sink_failures_file", str(sink_file))
    print(f"[sink-batch] since={since} failures={len(sink_failures)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument(
        "--listed",
        required=True,
        help="comma-separated workflow names whose failures this pass may post",
    )
    parser.add_argument("--previous-started", default="", help="run_started_at of the last pass")
    parser.add_argument("--overlap-minutes", type=int, default=120)
    parser.add_argument("--lookback-hours", type=int, default=3)
    parser.add_argument(
        "--reconcile-horizon-days",
        type=int,
        default=None,
        help="a reconcile pass: also list re-runs of runs created this many days back",
    )
    parser.add_argument(
        "--reconcile-price-days",
        type=int,
        default=None,
        help="a reconcile pass posts re-runs that completed this many days back",
    )
    parser.add_argument("--report-dir", type=Path, default=Path("."))
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    args = parser.parse_args()
    if (args.reconcile_horizon_days is None) != (args.reconcile_price_days is None):
        parser.error("--reconcile-horizon-days and --reconcile-price-days go together")
    if not args.token:
        parser.error("--token or GITHUB_TOKEN is required")
    if not args.listed.strip():
        parser.error("--listed is required")
    return run_sink_batch(args)


if __name__ == "__main__":
    raise SystemExit(main())
