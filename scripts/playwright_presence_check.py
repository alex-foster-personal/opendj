#!/usr/bin/env python3
"""Assert a Playwright JSON report executed every listed test.

Nightly webkit-deckload quarantine (#712) needs presence-based acceptance:
a run that fails early must still show every test as executed, not skipped.

Usage:
    playwright_presence_check.py [--floor N] REPORT.json

Exit codes:
    0 - every listed test executed; listed count meets floor when given
    1 - zero tests, floor miss, or any test did not run
    2 - missing/empty/invalid report file
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

EXECUTED = frozenset({"passed", "failed", "timedOut"})
DID_NOT_RUN = frozenset({"skipped", "interrupted"})


def _last_status(test: dict[str, Any]) -> str | None:
    results = test.get("results")
    if not isinstance(results, list) or not results:
        return None
    last = results[-1]
    if not isinstance(last, dict):
        return None
    status = last.get("status")
    return status if isinstance(status, str) else None


def _spec_test_rows(
    spec: dict[str, Any],
    file_label: str,
    project_name: str,
) -> list[tuple[str, str, str, str | None]]:
    spec_file = spec.get("file") or file_label
    spec_title = spec.get("title") or ""
    if not isinstance(spec_file, str):
        spec_file = file_label
    if not isinstance(spec_title, str):
        spec_title = ""
    tests = spec.get("tests")
    if not isinstance(tests, list):
        return []
    rows: list[tuple[str, str, str, str | None]] = []
    for test in tests:
        if not isinstance(test, dict):
            continue
        project = test.get("projectName") or project_name or "?"
        title = test.get("title") or spec_title or "?"
        if not isinstance(project, str):
            project = "?"
        if not isinstance(title, str):
            title = "?"
        rows.append((project, spec_file, title, _last_status(test)))
    return rows


def _walk_suites(
    suites: list[Any],
    *,
    file_title: str = "",
    project_name: str = "",
) -> list[tuple[str, str, str, str | None]]:
    """Return (project, file, title, status) for every test in nested suites."""
    rows: list[tuple[str, str, str, str | None]] = []
    for suite in suites:
        if not isinstance(suite, dict):
            continue
        next_file = suite.get("file") or suite.get("title") or file_title
        if isinstance(next_file, str):
            file_label = next_file
        else:
            file_label = file_title
        nested = suite.get("suites")
        if isinstance(nested, list) and nested:
            rows.extend(
                _walk_suites(nested, file_title=file_label, project_name=project_name)
            )
        specs = suite.get("specs")
        if not isinstance(specs, list):
            continue
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            rows.extend(_spec_test_rows(spec, file_label, project_name))
    return rows


def _load_report(path: Path) -> dict[str, Any]:
    if not path.is_file():
        print(f"playwright presence check: report missing: {path}", file=sys.stderr)
        raise SystemExit(2)
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        print(f"playwright presence check: report empty: {path}", file=sys.stderr)
        raise SystemExit(2)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"playwright presence check: report is not valid JSON ({exc}): {path}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    if not isinstance(data, dict):
        print(f"playwright presence check: report root is not an object: {path}", file=sys.stderr)
        raise SystemExit(2)
    return data


def _analyze(rows: list[tuple[str, str, str, str | None]]) -> tuple[int, int, int, int, list[str]]:
    passed = failed = timed_out = 0
    did_not_run_lines: list[str] = []
    for project, spec_file, title, status in rows:
        if status in EXECUTED:
            if status == "passed":
                passed += 1
            elif status == "failed":
                failed += 1
            else:
                timed_out += 1
            continue
        label = f"{project} › {spec_file} › {title}"
        if status in DID_NOT_RUN:
            did_not_run_lines.append(f"{label} ({status})")
        elif status is None:
            did_not_run_lines.append(f"{label} (no result)")
        else:
            did_not_run_lines.append(f"{label} ({status})")
    executed = passed + failed + timed_out
    listed = executed + len(did_not_run_lines)
    return listed, passed, failed, timed_out, did_not_run_lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when a Playwright JSON report lists tests that did not run."
    )
    parser.add_argument(
        "--floor",
        type=int,
        default=None,
        help="Minimum number of listed tests; catches silently truncated suites",
    )
    parser.add_argument("report", type=Path, help="Path to Playwright JSON report")
    args = parser.parse_args(argv)

    data = _load_report(args.report)
    suites = data.get("suites")
    if not isinstance(suites, list) or not suites:
        print("playwright presence check: report lists 0 tests", file=sys.stderr)
        return 1

    rows = _walk_suites(suites)
    listed, passed, failed, timed_out, did_not_run_lines = _analyze(rows)

    if listed == 0:
        print("playwright presence check: report lists 0 tests", file=sys.stderr)
        return 1

    if args.floor is not None and listed < args.floor:
        print(
            f"playwright presence check: report lists {listed} tests, floor is {args.floor}",
            file=sys.stderr,
        )
        return 1

    if did_not_run_lines:
        print(
            f"playwright presence check: {len(did_not_run_lines)} test(s) did not run:",
            file=sys.stderr,
        )
        for line in did_not_run_lines:
            print(f"  {line}", file=sys.stderr)
        return 1

    print(
        "presence ok: "
        f"{listed} executed ({passed} passed, {failed} failed, {timed_out} timedOut), "
        "0 did not run"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
