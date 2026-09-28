#!/usr/bin/env python3
"""Reap orphaned test leftovers that ``scripts/process_census.py`` classifies as reapable.

Run from a checkout: ``python -m scripts.orphan_reaper reap``. The census half
is stdlib only and runs on any host without a checkout (see its docstring);
this half only adds the kill, so the classification that decides WHAT dies has
one home.

``reap`` kills only ORPHANED trees with runner, agent or ``.test.`` harness
provenance, older than ``--min-age-s``, re-checking each pid's identity (start
time) before every signal so a recycled pid is never hit. Every kill is
reported with its evidence as one JSON line. A process with a non-test
``AF_SERVICE_ID`` is a service and is never a target.

    orphan_reaper.py census [--json]        # same as process_census.py census
    orphan_reaper.py reap [--dry-run] [--min-age-s N] [--report PATH] [--only-pid N]

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 reap kills orphaned attributed trees and never a service
    - [if] an orphaned `.test.` server survives `reap` [then ⛔️]
    - [if] a process whose root is a supervised service is signalled [then ⛔️]
    - [if] a recycled pid (start time changed) is signalled [then ⛔️]
  ✔︎ ✅ 🎯 reap reports counts so orphans are visible (JSON line to --report and stdout)
    - [if] reap kills N processes and the report says killed != N [then ⛔️]
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from scripts.process_census import (
    Proc,
    Row,
    classify,
    print_census,
    snapshot,
    snapshot_one,
    utc_now,
)


class RCFG:
    DEFAULT_MIN_AGE_S = 120  # younger than this may still have a live owner mid-teardown
    TERM_GRACE_S = 3.0


# ---------------------------------------------------------------- reap


def _same_process(pid: int, start: str) -> bool:
    fresh = snapshot_one(pid)
    return fresh is not None and fresh.start == start and not fresh.state.startswith("Z")


def reap(
    procs: dict[int, Proc],
    rows: list[Row],
    *,
    min_age_s: int,
    dry_run: bool,
    only_pids: list[int] | None = None,
) -> dict:
    targets = [
        r
        for r in rows
        if r.verdict == "reapable"
        and r.age_s >= min_age_s
        and (not only_pids or r.pid in only_pids)
    ]
    identities = {r.pid: procs[r.pid].start for r in targets}
    killed: list[dict] = []
    survivors: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        alive = [r for r in targets if _same_process(r.pid, identities[r.pid])]
        if sig == signal.SIGKILL and not alive:
            break
        for row in alive:
            if dry_run:
                continue
            try:
                os.kill(row.pid, sig)
            except ProcessLookupError:
                continue
            except PermissionError:
                survivors.append(row.pid)
        if dry_run:
            break
        time.sleep(RCFG.TERM_GRACE_S if sig == signal.SIGTERM else 0.5)
    for row in targets:
        still = (not dry_run) and _same_process(row.pid, identities[row.pid])
        if still:
            survivors.append(row.pid)
        killed.append(
            {
                "pid": row.pid,
                "attribution": row.attribution,
                "root_pid": row.root_pid,
                "age_s": row.age_s,
                "cwd": row.cwd,
                "command": row.command[:160],
                "env": row.env,
                "outcome": "would-kill" if dry_run else ("SURVIVED" if still else "killed"),
            }
        )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.verdict] = counts.get(row.verdict, 0) + 1
    return {
        "at": utc_now(),
        "host": os.uname().nodename,
        "dry_run": dry_run,
        "in_scope": len(rows),
        "verdicts": counts,
        "reapable": len(targets),
        "killed": sum(1 for k in killed if k["outcome"] == "killed"),
        "survived": sorted(set(survivors)),
        "kills": killed,
    }


# ---------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    census = sub.add_parser("census", help="measure; never kills")
    census.add_argument("--json", action="store_true")
    reaper = sub.add_parser("reap", help="kill orphaned, attributed test leftovers")
    reaper.add_argument("--dry-run", action="store_true")
    reaper.add_argument("--min-age-s", type=int, default=RCFG.DEFAULT_MIN_AGE_S)
    reaper.add_argument("--report", type=Path, help="append the JSON report line here")
    reaper.add_argument(
        "--only-pid",
        type=int,
        action="append",
        default=[],
        help="restrict kills to these pids (repeatable)",
    )
    args = parser.parse_args(argv)

    procs = snapshot()
    rows = classify(procs)
    if args.cmd == "census":
        print_census(procs, rows, as_json=args.json)
        exit_code = 0
    elif args.cmd == "reap":
        report = reap(
            procs, rows, min_age_s=args.min_age_s, dry_run=args.dry_run, only_pids=args.only_pid
        )
        line = json.dumps(report)
        print(line)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with args.report.open("a") as fh:
                fh.write(line + "\n")
        exit_code = 1 if report["survived"] else 0
    else:
        raise SystemExit(f"unknown command {args.cmd!r}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
