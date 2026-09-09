"""Prove every check in :mod:`scripts.sync_drift_lint` can still FAIL.

The drift linter guards OMISSIONS, and an omission raises nothing. That makes
a green run indistinguishable from a linter pointed at the wrong thing unless
each check has been SHOWN to fire (``.claude/rules/verification.md``). The
per-check tests next to it do that at the assertion level; this does it at the
whole-file level, by injecting the exact defect the file exists to prevent:
one check made to ``return []`` before it measures anything.

Run it after touching the linter, the subject, or the tests:

    uv run --no-sync python -m scripts.sync_drift_mutation_control

It edits ``scripts/sync_drift_lint.py`` in place, one function at a time, and
restores it in a ``finally``. Exit 0 means every check was caught by its own
named tests.

THE HARNESS NEEDED A CONTROL OF ITS OWN, AND THE FIRST VERSION FAILED IT.
Every mutation inserts the SAME 15 bytes into the SAME file, so each mutated
file has an IDENTICAL SIZE, and consecutive iterations land inside one
filesystem-mtime second. CPython validates a cached ``.pyc`` on (mtime
seconds, size), so both matched and pytest re-imported the PREVIOUS
iteration's bytecode: the run for check N reported the failures of check N-1,
every row still showed non-zero failures, and the table read as eight detected
mutations. Two defenses, both kept:

* ``PYTHONDONTWRITEBYTECODE`` plus a swept ``__pycache__``, so no stale
  bytecode can be validated as current.
* A control DERIVED FROM THE HYPOTHESIS rather than from a clean corpus.
  Blinding check X predicts that X's OWN tests fail, so :data:`EXPECTED`
  names the rule token each check's tests carry and the run aborts when
  something else fails instead. That assertion fails immediately under the
  stale-bytecode bug, which is the whole point: "some tests went red" is the
  same trap as "no failures found", one level up.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO: Path = Path(__file__).resolve().parent.parent
LINT: Path = REPO / "scripts" / "sync_drift_lint.py"
PYCACHE: Path = LINT.parent / "__pycache__"
TESTS: tuple[str, ...] = (
    "tests/quality/test_sync_drift_lint.py",
    "tests/quality/test_sync_drift_declarations.py",
    "tests/quality/test_sync_drift_floors.py",
)

EXPECTED: dict[str, str] = {
    "check_unregistered_synced_table": "d01",
    "check_registered_table_missing": "d02",
    "check_version_ladder_mismatch": "d03",
    "check_undocumented_table_or_column": "d04",
    "check_naive_stamp_default": "d05",
    "check_mirror_version_mismatch": "d06",
    "check_migration_step_changed": "d07",
    "check_undeclared_state_table": "d08",
}
"""Check -> the rule token its OWN tests are named after.

This is the hypothesis, written down: blinding a check predicts a failure
HERE and nowhere else. A control that cannot fail for the reason under test
is not a control, and this one can: it went red the moment stale bytecode
made a run report the previous check's failures."""


class MutationControlError(RuntimeError):
    """The harness measured something other than what it claimed to."""


def _run_tests() -> tuple[int, list[str]]:
    """Run the drift tests with no bytecode cache; return (exit code, failures)."""
    shutil.rmtree(PYCACHE, ignore_errors=True)
    proc = subprocess.run(
        ["uv", "run", "--no-sync", "pytest", *TESTS, "-q", "--no-header"],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
    )
    failed = sorted(
        line.split(" ", 2)[1].split("::")[-1]
        for line in proc.stdout.splitlines()
        if line.startswith("FAILED ")
    )
    return proc.returncode, failed


def _blind(source: str, name: str) -> str:
    """``source`` with ``name`` returning [] as its first statement.

    Asserts the edit added exactly one line and landed in that function, so a
    regex that silently matched a neighbour cannot masquerade as a mutation.
    """
    blinded = re.sub(
        rf"^(def {name}\([^)]*\) -> list\[Violation\]:\n)",
        r"\1    return []\n",
        source,
        count=1,
        flags=re.MULTILINE,
    )
    diff = list(difflib.unified_diff(source.splitlines(), blinded.splitlines(), lineterm="", n=1))
    added = [ln for ln in diff if ln.startswith("+") and not ln.startswith("+++")]
    if added != ["+    return []"]:
        raise MutationControlError(f"{name}: mutation changed more than one line: {added}")
    if not any(name in line for line in diff):
        raise MutationControlError(f"{name}: the mutation did not land in that function")
    return blinded


def main() -> int:
    original = LINT.read_text(encoding="utf-8")
    checks = re.findall(r"^def (check_[a-z_]+)\(", original, re.MULTILINE)
    if sorted(checks) != sorted(EXPECTED):
        raise MutationControlError(
            f"the linter defines {sorted(checks)} but this harness expects "
            f"{sorted(EXPECTED)}. Update EXPECTED deliberately: a check with "
            "no entry here would never be blinded and never be proven."
        )

    code, failed = _run_tests()
    if code != 0 or failed:
        raise MutationControlError(
            f"the unmutated tree is already red ({failed}), so nothing below "
            "would mean anything."
        )
    print("BASELINE: green, so a red below is caused by the mutation.\n")

    rows: list[tuple[str, list[str]]] = []
    try:
        for name in checks:
            LINT.write_text(_blind(original, name), encoding="utf-8")
            code, failed = _run_tests()
            if code == 0:
                raise MutationControlError(
                    f"{name}: pytest exited 0 with the check blinded. That "
                    "mutation is UNDETECTED, so the check is not guarded."
                )
            stray = [t for t in failed if EXPECTED[name] not in t]
            if stray:
                raise MutationControlError(
                    f"{name}: blinding it predicted a failure in its own "
                    f"{EXPECTED[name]} tests, but these failed instead: "
                    f"{stray}. The harness is measuring the wrong subject."
                )
            rows.append((name, failed))
            print(f"{name}: exit {code}, {len(failed)} failed -> {', '.join(failed)}")
    finally:
        LINT.write_text(original, encoding="utf-8")
        shutil.rmtree(PYCACHE, ignore_errors=True)

    code, failed = _run_tests()
    if code != 0 or failed:
        raise MutationControlError(f"the tree did not restore to green: {failed}")
    print("\nRESTORED and green again.\n")

    print("| check blinded (returns [] first) | tests that failed | names |")
    print("| --- | --- | --- |")
    for name, names in rows:
        print(f"| `{name}` | {len(names)} | {', '.join(names)} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
