"""Decide one fast-lane pytest shard's verdict with Trunk's quarantine applied.

the maintainer, Wed 30 Sep 2026: "Set TRUNK QUARANTINE to True so flakey tests stop
blocking merges so aggressively." ADR-NEW-trunk-flaky-quarantine-on.

The shard's pytest step runs with continue-on-error and records its exit code;
this script is the step that turns that exit code into the job's verdict. It
lets a failing shard through ONLY when every one of these holds:

- pytest exited exactly 1 (tests ran and some failed). 2 (interrupted or
  collection errors), 3 (internal error), 4 (usage error, which is how the
  --collect-floor and --ledger-coverage-min gates fail), 5 (nothing
  collected), and 124/137 (the shard's wall budget) always fail.
- the quarantine list arrived from the isolated `trunk-quarantine-list` job,
  parsed, and says status "ok". No list means nothing is quarantined.
- the list is not implausibly long (MAX_QUARANTINED_LIST). A list that size is
  a mass false flag, the Wed 30 Sep 2026 incident flagged 1787 tests.
- the JUnit report exists, parses, and records at least one failed testcase
  (a <failure> or <error> child). Exit 1 with nothing failed is unexplained.
- every failed testcase is on the list, matched on (classname, name).
- no more than MAX_EXCUSED_PER_SHARD failures are excused at once. More than
  that in one shard is an infrastructure burst, not a flake.

Why not Trunk's own CLI: it turns ANY non-zero exit into a pass when every
recorded failure is quarantined, timeouts and collection errors included.

Requirements (REQ CIQUAR-01):
  ✔︎ exit 0 passes without consulting the list
    - [if] pytest exit 0 and no list at all [then ⛔️ if the verdict fails]
  ✔︎ an unlisted failure fails
    - [if] exit 1, one failure, list empty [then ⛔️ if the verdict passes]
    - [if] exit 1, two failures, only one listed [then ⛔️ if the verdict passes]
  ✔︎ a listed-only failure passes
    - [if] exit 1, every failure listed [then ⛔️ if the verdict fails]
  ✔︎ any exit other than 0 or 1 fails even when every failure is listed
    - [if] exit 2, 4, 124 or 137 with listed failures [then ⛔️ if the verdict passes]
  ✔︎ a missing, unparseable or not-ok list fails closed
    - [if] exit 1, list empty string [then ⛔️ if the verdict passes]
    - [if] exit 1, list status "unreachable" [then ⛔️ if the verdict passes]
  ✔︎ a missing report, or exit 1 with no failed testcase, fails
    - [if] exit 1 and the report is absent [then ⛔️ if the verdict passes]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

#: pytest's "tests ran and some failed". The only exit the list may excuse.
PYTEST_TESTS_FAILED = 1
#: ~2% of the ~17,800 tests the five fast-lane shards collect. A longer list is
#: a mass false flag (1787 on Wed 30 Sep 2026), so nothing is excused.
MAX_QUARANTINED_LIST = 350
#: More failures than this in one shard is an infrastructure burst.
MAX_EXCUSED_PER_SHARD = 10


# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Verdict:
    passed: bool
    reason: str
    excused: tuple[str, ...] = ()


TestKey = tuple[str, str]


# -----------------------------------------------------------------------------
def _parse_quarantine_list(raw: str) -> frozenset[TestKey] | str:
    """Return the quarantined (classname, name) keys, or a reason they are unusable."""
    if not raw.strip():
        return "no quarantine list reached this job (the list job was skipped, failed, or is switched off)"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        return f"the quarantine list is not JSON ({error})"
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        status = payload.get("status") if isinstance(payload, dict) else type(payload).__name__
        return f"the quarantine list status is {status!r}, not 'ok'"
    tests = payload.get("tests")
    if not isinstance(tests, list):
        return "the quarantine list has no 'tests' array"
    if len(tests) > MAX_QUARANTINED_LIST:
        return f"the quarantine list holds {len(tests)} tests, over the {MAX_QUARANTINED_LIST} cap, so it is treated as a mass false flag"
    keys = set()
    for entry in tests:
        if not isinstance(entry, dict) or not entry.get("c") or not entry.get("n"):
            return f"the quarantine list has a malformed entry {entry!r}"
        keys.add((str(entry["c"]), str(entry["n"])))
    return frozenset(keys)


def _failed_testcases(junit: Path) -> list[TestKey] | str:
    """Return every testcase with a <failure> or <error> child, or a reason the report is unusable."""
    if not junit.is_file():
        return f"the JUnit report {junit} does not exist"
    try:
        root = ET.parse(junit).getroot()
    except ET.ParseError as error:
        return f"the JUnit report does not parse ({error})"
    return [
        (case.get("classname", ""), case.get("name", ""))
        for case in root.iter("testcase")
        if case.find("failure") is not None or case.find("error") is not None
    ]


# -----------------------------------------------------------------------------
def decide(pytest_rc: str, quarantine_raw: str, junit: Path) -> Verdict:
    if pytest_rc == "0":
        return Verdict(True, "pytest exited 0")
    if pytest_rc != str(PYTEST_TESTS_FAILED):
        return Verdict(
            False,
            f"pytest exited {pytest_rc or 'with no recorded code'}; only exit 1 can be excused by quarantine",
        )
    quarantined = _parse_quarantine_list(quarantine_raw)
    if isinstance(quarantined, str):
        return Verdict(False, f"nothing is quarantined: {quarantined}")
    failed = _failed_testcases(junit)
    if isinstance(failed, str):
        return Verdict(False, failed)
    if not failed:
        return Verdict(False, "pytest exited 1 but the JUnit report records no failed testcase")
    unlisted = [f"{c}::{n}" for c, n in failed if (c, n) not in quarantined]
    if unlisted:
        return Verdict(
            False,
            f"{len(unlisted)} failed test(s) are not quarantined: " + ", ".join(unlisted[:20]),
        )
    if len(failed) > MAX_EXCUSED_PER_SHARD:
        return Verdict(
            False,
            f"{len(failed)} quarantined failures in one shard exceeds {MAX_EXCUSED_PER_SHARD}; treated as an infrastructure burst",
        )
    excused = tuple(f"{c}::{n}" for c, n in failed)
    return Verdict(True, f"all {len(failed)} failure(s) are quarantined in Trunk", excused)


# -----------------------------------------------------------------------------
def _report(verdict: Verdict, shard: str) -> None:
    if verdict.passed and verdict.excused:
        print(
            f"::warning title=Trunk quarantine excused {len(verdict.excused)} failure(s) (shard {shard} of 5)::{', '.join(verdict.excused)}"
        )
    elif not verdict.passed:
        print(f"::error title=pytest fast lane FAILED (shard {shard} of 5)::{verdict.reason}")
    print(f"VERDICT: {'PASS' if verdict.passed else 'FAIL'} -- {verdict.reason}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(
                f"### Fast lane verdict (shard {shard} of 5): {'PASS' if verdict.passed else 'FAIL'}\n\n{verdict.reason}\n\n"
            )
            for test in verdict.excused:
                handle.write(f"- quarantined: `{test}`\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--pytest-rc", required=True, help="the shard's pytest exit code, as recorded by its step"
    )
    parser.add_argument("--junit", required=True, type=Path)
    parser.add_argument("--shard", required=True)
    parser.add_argument(
        "--quarantine-env", default="TRUNK_QUARANTINE_LIST", help="env var holding the list JSON"
    )
    args = parser.parse_args(argv)
    verdict = decide(args.pytest_rc, os.environ.get(args.quarantine_env, ""), args.junit)
    _report(verdict, args.shard)
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    sys.exit(main())
