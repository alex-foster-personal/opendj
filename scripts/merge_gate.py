#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Pre-merge gate for music-dj-tools re-land work.

Every check maps to a failure mode observed in the Jul-Aug 2026 discipline audit.
Fail-fast: any FAIL exits non-zero. There is no "pre-existing" allowance -- that
waiver is precisely how the svelte-check gate stopped being a gate.

Requirements (mini-PRD)
  1. No untracked source files                                         [OK] done
     - if a .py/.ts/.svelte/.mjs source file is untracked then FAIL
     - if only build artefacts or ignored paths are untracked then PASS
     - rationale: 53 untracked files (11 of them tests) had zero git protection
  2. Every source change is accompanied by a test change               [OK] done
     - if the range touches apps/ source without touching tests then FAIL
     - if the range is docs/chore only then PASS
     - rationale: TDD rate fell 77 pct -> 39 pct after Sat 25 Jul 2026
  3. Architecture doc tracks the code                                  [OK] done
     - if a new top-level module appears and architecture.md is untouched then FAIL
     - rationale: architecture.md went stale Fri 17 Apr and nobody noticed
  4. Conventional commit subjects                                      [OK] done
     - if a subject is not <type>(<scope>): <text> then FAIL
     - if the subject is a stash blob ("On <branch>:" / "WIP on") then FAIL
     - rationale: state docs were last written by an accidental stash commit

Usage
  ./scripts/merge_gate.py --range <base>..<head>
  ./scripts/merge_gate.py --range HEAD~1..HEAD --skip architecture
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field

# -----------------------------------------------------------------------------
# CFG
# -----------------------------------------------------------------------------

SOURCE_SUFFIXES: tuple[str, ...] = (".py", ".ts", ".svelte", ".mjs")
TEST_PATTERN = re.compile(r"(^|/)tests?/|_test\.|\.test\.|\.spec\.")
SOURCE_ROOTS: tuple[str, ...] = ("apps/", "scripts/")
ARCHITECTURE_DOC = "docs/architecture.md"
# Floor ratchets upward with each round, never silently. 70 = the Jul 21-25 level,
# measured before the Sat 25 Jul 2026 collapse to 39.
TDD_FLOOR_DEFAULT = 70
CONVENTIONAL = re.compile(r"^(feat|fix|test|docs|chore|refactor|perf|revert|build|ci)(\([^)]+\))?: .+")
STASH_BLOB = re.compile(r"^(On [^:]+:|WIP on )")
MERGE_SUBJECT = re.compile(r"^(Merge |Revert )")


@dataclass
class Result:
    name: str
    passed: bool
    detail: list[str] = field(default_factory=list)


# -----------------------------------------------------------------------------
# _helpers
# -----------------------------------------------------------------------------


def _git(*args: str) -> str:
    proc = subprocess.run(("git", *args), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _changed_files(rev_range: str) -> list[str]:
    return [f for f in _git("diff", "--name-only", rev_range).splitlines() if f]


def _subjects(rev_range: str) -> list[tuple[str, str]]:
    raw = _git("log", "--no-merges", "--format=%h|%s", rev_range).splitlines()
    return [tuple(line.split("|", 1)) for line in raw if "|" in line]  # type: ignore[misc]


def _is_source(path: str) -> bool:
    return path.endswith(SOURCE_SUFFIXES) and not TEST_PATTERN.search(path)


def _is_test(path: str) -> bool:
    return bool(TEST_PATTERN.search(path))


# -----------------------------------------------------------------------------
# checks
# -----------------------------------------------------------------------------


def check_no_untracked_source() -> Result:
    untracked = [f for f in _git("ls-files", "--others", "--exclude-standard").splitlines() if f]
    orphans = [f for f in untracked if f.endswith(SOURCE_SUFFIXES)]
    if orphans:
        return Result(
            "no-untracked-source",
            False,
            [f"{len(orphans)} source files exist only on disk, not in git:"]
            + [f"    {f}" for f in orphans[:15]]
            + ([f"    ... and {len(orphans) - 15} more"] if len(orphans) > 15 else []),
        )
    return Result("no-untracked-source", True, [f"{len(untracked)} untracked paths, none of them source"])


def check_test_accompanies_source(rev_range: str, floor: int) -> Result:
    """Per-COMMIT TDD rate, not per-range.

    Range-level counting is a false negative: perf-sync-ux/00..03 touched 6 test
    files in aggregate and still shipped untested feature commits. The discipline
    that actually degraded was per-commit, so it has to be measured there.
    """
    feature_commits = [
        (sha, subject)
        for sha, subject in _subjects(rev_range)
        if not MERGE_SUBJECT.match(subject) and re.match(r"^(feat|fix)", subject)
    ]
    if not feature_commits:
        return Result("test-accompanies-source", True, ["no feat/fix commits in range"])

    untested: list[str] = []
    for sha, subject in feature_commits:
        files = [f for f in _git("show", "--name-only", "--format=", sha).splitlines() if f]
        touches_source = any(_is_source(f) and f.startswith(SOURCE_ROOTS) for f in files)
        touches_test = any(_is_test(f) for f in files)
        if touches_source and not touches_test:
            untested.append(f"    {sha} {subject[:62]}")

    tested = len(feature_commits) - len(untested)
    rate = round(tested * 100 / len(feature_commits))
    summary = f"TDD rate {rate}% ({tested}/{len(feature_commits)} feat/fix commits carry tests), floor {floor}%"

    # Two legitimate disciplines, both observed in the good era:
    #   (a) every feat/fix commit carries its own test       -> rate >= floor
    #   (b) the sequence is CLOSED by a test(...) commit     -> settings-panel/01..04
    # perf-sync-ux/00..03 satisfied neither, and that is the regression.
    closing_test = [
        f"{sha} {subject[:62]}"
        for sha, subject in _subjects(rev_range)
        if re.match(r"^test(\([^)]+\))?: ", subject)
    ]
    if closing_test:
        return Result(
            "test-accompanies-source",
            True,
            [summary, f"sequence closed by test commit: {closing_test[-1]}"],
        )
    if rate < floor:
        return Result(
            "test-accompanies-source",
            False,
            [summary, "no closing test(...) commit, and rate is under floor", "untested:"] + untested[:12],
        )
    return Result("test-accompanies-source", True, [summary])


def check_architecture_tracks_code(rev_range: str) -> Result:
    changed = _changed_files(rev_range)
    new_modules = {
        f.split("/")[1]
        for f in changed
        if f.startswith("apps/") and len(f.split("/")) > 2
    }
    known = {
        d.split("/")[1]
        for d in _git("ls-tree", "-d", "--name-only", "-r", f"{rev_range.split('..')[0]}", "apps").splitlines()
        if len(d.split("/")) > 1
    }
    added = new_modules - known
    if added and ARCHITECTURE_DOC not in changed:
        return Result(
            "architecture-tracks-code",
            False,
            [f"new module(s) {sorted(added)} but {ARCHITECTURE_DOC} untouched"],
        )
    return Result("architecture-tracks-code", True, ["no new top-level modules, or doc updated alongside"])


def check_commit_hygiene(rev_range: str) -> Result:
    bad: list[str] = []
    for sha, subject in _subjects(rev_range):
        if MERGE_SUBJECT.match(subject):
            continue
        if STASH_BLOB.match(subject):
            bad.append(f"    {sha} STASH BLOB: {subject[:60]}")
        elif not CONVENTIONAL.match(subject):
            bad.append(f"    {sha} NOT CONVENTIONAL: {subject[:60]}")
    if bad:
        return Result("commit-hygiene", False, [f"{len(bad)} malformed subjects:"] + bad[:15])
    return Result("commit-hygiene", True, ["all subjects conventional, no stash blobs"])


# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-merge gate. Exits non-zero on any FAIL.")
    parser.add_argument("--range", dest="rev_range", required=True, help="git rev range, e.g. main..HEAD")
    parser.add_argument("--skip", nargs="*", default=[], help="check names to skip (use sparingly, and say why)")
    parser.add_argument(
        "--tdd-floor",
        type=int,
        default=TDD_FLOOR_DEFAULT,
        help=f"minimum pct of feat/fix commits carrying tests (default {TDD_FLOOR_DEFAULT}, the Jul 21-25 level)",
    )
    args = parser.parse_args()

    checks: list[Result] = []
    if "untracked" not in args.skip:
        checks.append(check_no_untracked_source())
    if "tests" not in args.skip:
        checks.append(check_test_accompanies_source(args.rev_range, args.tdd_floor))
    if "architecture" not in args.skip:
        checks.append(check_architecture_tracks_code(args.rev_range))
    if "hygiene" not in args.skip:
        checks.append(check_commit_hygiene(args.rev_range))

    print(f"\nmerge gate :: range {args.rev_range}\n" + "-" * 68)
    for r in checks:
        print(f"[{'OK' if r.passed else 'FAIL'}] {r.name}")
        for line in r.detail:
            print(f"       {line}")
    failed = [r for r in checks if not r.passed]
    print("-" * 68)
    if args.skip:
        print(f"[WARN] skipped: {', '.join(args.skip)} -- a skipped gate is a waived gate")
    if failed:
        print(f"[FAIL] {len(failed)} of {len(checks)} gates failed. Merge blocked.\n")
        return 1
    print(f"[OK] all {len(checks)} gates passed.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
