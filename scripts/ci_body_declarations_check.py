"""Fail a PR in seconds when its body's `CI-Tests:` or `CI-Wide-Reason:` lines would be refused.

Thu 5 Nov 2026: #8822, #8857, #8761 and #9003 each lost a full CI cycle (about 10 minutes of
shards, then a close/reopen) to one malformed `CI-Tests:` line, which the selector refuses with
HARD CAP REFUSED only once the shards run. This check runs in the `pr-body-gates` workflow on
every PR event, `edited` included, with the selector's own parser and path rules imported, never
copied, so the owner learns the fix before any shard starts.

Requirements (mini-PRD):
  / Every `CI-Tests:` line is `CI-Tests: <path> -- <reason>` (scripts.ci_test_selection
    parse_declarations).
    [if] `CI-Tests: tests/x/test_y.py` with no ` -- <reason>` passes [then broken]
  / Every declared path is one the selector accepts at this checkout: an existing frontend test
    (split_frontend_declarations), an existing lane-ignored file (split_lane_ignored_declarations)
    or an existing tests/**/test_*.py module (undeclarable is empty).
    [if] a path that is not a pytest module passes [then broken]
    [if] an existing frontend test is refused [then broken]
    [if] a lane-ignored test named under ci.yml's `--ignore=` is refused [then broken]
  / A `CI-Wide-Reason:` line counts only at MIN_REASON_CHARS (scripts.ci_selection_guard
    written_reason), so a body whose every such line is shorter fails.
    [if] `CI-Wide-Reason: wf` alone passes [then broken]
  / The lane ignores are the `--ignore=` tokens of .github/workflows/ci.yml, the list the scope
    job hands the selector; none found is exit 2 (cannot measure), never a pass.
  / Each failure prints the bad line and its fixed form.

Usage: `python3 -m scripts.ci_body_declarations_check --body-file F`. Exit 0 pass, 1 fail,
2 could not measure. Standard library only, like the selector it imports.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from scripts.affected_tests import REPO
from scripts.ci_selection_guard import _WIDE_REASON, MIN_REASON_CHARS, written_reason
from scripts.ci_test_selection import (
    _DECLARATION,
    _DECLARATION_LINE,
    parse_declarations,
    split_frontend_declarations,
    split_lane_ignored_declarations,
    undeclarable,
)

CI_WORKFLOW = ".github/workflows/ci.yml"
_IGNORE = re.compile(r"^[ \t]*--ignore=(\S+)[ \t]*\\?[ \t]*$", re.M)


def lane_ignores(workflow: Path) -> list[str]:
    """The `--ignore=` paths ci.yml hands the selector (the scope, fast and shard steps carry the
    same list, pinned by tests/scripts/test_ci_fast_tier_job.py), in order, deduplicated."""
    return list(dict.fromkeys(_IGNORE.findall(workflow.read_text(encoding="utf-8"))))


def _normalized(body: str) -> str:
    return body.replace("\r\n", "\n").replace("\r", "\n")


def _line_of(body: str, path: str) -> str:
    for match in _DECLARATION_LINE.finditer(body):
        if match["rest"].split()[:1] == [path]:
            return match.group(0).strip()
    return f"CI-Tests: {path} -- ..."


def problems(body: str, ignores: list[str], root: Path) -> list[str]:
    """One message per refused line: the line, why, and the fixed form."""
    text = _normalized(body)
    out: list[str] = []
    declared, _errors = parse_declarations(text)
    for match in _DECLARATION_LINE.finditer(text):
        if _DECLARATION.match(match["rest"]):
            continue
        words = match["rest"].split()
        path = words[0] if words and not words[0].startswith("--") else "tests/<area>/test_<name>.py"
        out.append(
            f"`{match.group(0).strip()}`: malformed, expected `CI-Tests: <path> -- <reason>` "
            f"(space, two hyphens, space, a reason). Fixed form: `CI-Tests: {path} -- <why this PR needs it>`"
        )
    pytest_side, _frontend = split_frontend_declarations(declared, root)
    kept, _ignored = split_lane_ignored_declarations(pytest_side, ignores, root)
    for path in undeclarable(kept, ignores, root):
        module = path.split("::", 1)[0]
        if module != path:
            why, fix = "a node id: the selector takes whole modules only", f"CI-Tests: {module} -- <reason>"
        elif not (root / path).is_file():
            why = "not a file in this PR's tree (typo, renamed or deleted)"
            fix = "CI-Tests: <the tests/**/test_*.py path as it is in the tree> -- <reason>, or delete the line"
        else:
            why = "not a pytest module (`tests/**/test_*.py`), and no lane ignore in ci.yml covers it"
            fix = "CI-Tests: <the tests/**/test_*.py module that covers it> -- <reason>, or delete the line"
        out.append(f"`{_line_of(text, path)}`: {why}. Fixed form: `{fix}`")
    wide = [m.group(0).strip() for m in _WIDE_REASON.finditer(text)]
    if wide and written_reason(text) is None:
        out.append(
            f"`{wide[0]}`: shorter than {MIN_REASON_CHARS} characters, so it is never read as a "
            "reason. Fixed form: `CI-Wide-Reason: <why this change needs this many tests, at least "
            f"{MIN_REASON_CHARS} characters>`"
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--root", default=str(REPO))
    arguments = parser.parse_args(argv)
    root = Path(arguments.root)
    ignores = lane_ignores(root / CI_WORKFLOW)
    if not ignores:
        print(f"[UNKNOWN] no `--ignore=` tokens in {CI_WORKFLOW}: the lane cannot be read", file=sys.stderr)
        return 2
    found = problems(Path(arguments.body_file).read_text(encoding="utf-8"), ignores, root)
    for line in found:
        print(f"::error title=CI-Tests body line refused::{line}")
        print(f"[FAIL] {line}")
    if found:
        print(
            f"[FAIL] {len(found)} body line(s) the test selector refuses; edit the PR body (no push "
            "needed: this check re-runs on `edited`). Grammar: (private path)"
        )
        return 1
    print("[PASS] every `CI-Tests:` and `CI-Wide-Reason:` line reads as the selector reads it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
