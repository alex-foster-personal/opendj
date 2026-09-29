#!/usr/bin/env python3
"""Stable evidence's scheduled pass: append every recorded completion since a mark.

RUN-COUNT round 2 (issue #2196): one scheduled job instead of one workflow_run
job per completion. The exclusions the per-completion job carried in its `if`
live here in `select_suite_runs`, beside the suite map they belong to. The
evidence file, its writer and the release gate that reads it are unchanged
(scripts/stable_evidence.py, OPS-18).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.ci_run_batch import batch_since, created_floor, fetch_completed_runs
from scripts.stable_evidence import (
    append_suite,
    default_evidence_dir,
    evidence_path,
    load_evidence,
    suite_for_workflow,
)

RECORDED_WORKFLOWS: frozenset[str] = frozenset(
    {"CI", "Full CI (on-demand)", "E2E", "macOS Packaging"}
)


def _is_recordable(run: dict[str, Any]) -> bool:
    if run.get("name") not in RECORDED_WORKFLOWS or run.get("status") != "completed":
        return False
    if run.get("conclusion") == "cancelled":
        # A cancelled run has no suite result (Mon 14 Sep 2026: 27 of 27 appends
        # in one hour were of cancelled runs while 39/39 runners were busy).
        return False
    # A `CI` run started by workflow_dispatch is never evidence for its sha
    # (Codex P1 on #3651, issue #3582): main-control dispatches `tier: fast`
    # every six hours, which runs only the pytest fast tier under the name `CI`.
    return not (run.get("name") == "CI" and run.get("event") == "workflow_dispatch")


def select_suite_runs(runs: Iterable[dict[str, Any]], since: str) -> list[dict[str, Any]]:
    """The newest recordable completion per sha and suite, updated at or after
    `since`, oldest first.

    Coalescing happens BEFORE any write: two completions for one sha and suite
    in one window would otherwise be written in turn, and a pass that dies
    between the two leaves the older result in the file (Codex P1 on #3844).
    A re-run of the same run id is a later completion of that id and wins.
    """
    chosen = [r for r in runs if _is_recordable(r) and str(r.get("updated_at") or "") >= since]
    chosen.sort(key=lambda r: (str(r.get("updated_at")), int(r["id"])))
    newest: dict[tuple[str, str], dict[str, Any]] = {}
    for run in chosen:
        newest[(str(run["head_sha"]), suite_for_workflow(str(run["name"])))] = run
    return sorted(newest.values(), key=lambda r: (str(r.get("updated_at")), int(r["id"])))


def _already_recorded(evidence_dir: Path, run: dict[str, Any], suite: str) -> bool:
    path = evidence_path(evidence_dir, str(run["head_sha"]))
    if not path.is_file():
        return False
    record = (load_evidence(path).get("suites") or {}).get(suite)
    return record == {"run_id": str(run["id"]), "conclusion": str(run["conclusion"])}


def append_suite_runs(
    evidence_dir: Path, runs: list[dict[str, Any]], written_by_prefix: str
) -> list[tuple[dict[str, Any], Path]]:
    """Append each run in order; a run already recorded identically is left
    alone, so re-applying an overlap between two passes writes nothing."""
    written: list[tuple[dict[str, Any], Path]] = []
    for run in runs:
        suite = suite_for_workflow(str(run["name"]))
        if _already_recorded(evidence_dir, run, suite):
            continue
        path = append_suite(
            evidence_dir,
            str(run["head_sha"]),
            suite=suite,
            run_id=str(run["id"]),
            conclusion=str(run["conclusion"]),
            written_by=f"{written_by_prefix}:{run['name']}",
        )
        written.append((run, path))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.stable_evidence_batch",
        description="merge every recorded CI completion since the previous pass (OPS-18)",
    )
    parser.add_argument("--repository", required=True)
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    parser.add_argument("--previous-started", default="")
    parser.add_argument("--overlap-minutes", type=int, default=30)
    parser.add_argument("--lookback-hours", type=int, default=6)
    parser.add_argument("--written-by", default="github-actions")
    parser.add_argument("--evidence-dir", default=None)
    args = parser.parse_args(argv)

    since = batch_since(
        args.previous_started or None, datetime.now(UTC), timedelta(minutes=args.overlap_minutes)
    )
    created_since = created_floor(since, timedelta(hours=args.lookback_hours))
    listed = fetch_completed_runs(
        args.repository, created_since, args.token, "stable-evidence",
        workflow_names=RECORDED_WORKFLOWS,
    )
    chosen = select_suite_runs(listed, since)
    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else default_evidence_dir()
    written = append_suite_runs(evidence_dir, chosen, args.written_by)
    for run, path in written:
        print(f"[OK] run {run['id']} {run['name']} {run['conclusion']} -> {path}", file=sys.stderr)
    print(
        f"[OK] since={since} listed={len(listed)} recordable={len(chosen)} written={len(written)}",
        file=sys.stderr,
    )
    output_file = os.environ.get("GITHUB_OUTPUT", "")
    if output_file:
        with Path(output_file).open("a", encoding="utf-8") as handle:
            handle.write(f"since={since}\nrecordable={len(chosen)}\nwritten={len(written)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
