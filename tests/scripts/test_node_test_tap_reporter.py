"""Every `node --test` invocation must use the TAP reporter.

Node's default `spec` reporter joins every failed and cancelled result into one
string to print the summary. A large cancellation cascade overflows V8's
maximum string length and the reporter dies with `RangeError: Invalid string
length` WHILE PRINTING THE FAILURE, so nothing is printed at all and the real
failure is invisible. A missed invocation is therefore worse than an ordinary
missed fix: the gate still goes red, but it goes red with no test identities
(see issue #3466, and the Codex BLOCKING finding on PR #3468).

This guards the CLASS rather than the sites known when it was written -- a
seventh invocation added later is caught here instead of at the next cascade.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Executable surfaces only. specs/ and .planning/ are write-once prose records
# and are deliberately out of scope.
SEARCH_PATHS = (
    "Makefile",
    "justfile",
    "apps/webui/frontend/package.json",
    "scripts",
    ".github/workflows",
)

# A line is an INVOCATION rather than prose about one when it names a test file
# or sets a runner flag. The module docstring of savepoint_gate.py mentions
# `node --test` in a sentence and must not be mistaken for a call site.
# IGNORECASE because the Makefile calls `$(FRONTEND_NODE) --test`, and a
# case-sensitive `node` would silently skip that site entirely.
_INVOCATION = re.compile(r"node.{0,40}--test\b", re.IGNORECASE)
_IS_REAL_CALL = re.compile(r"\.test\.mjs|--test-concurrency")


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    for entry in SEARCH_PATHS:
        target = REPO_ROOT / entry
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(p for p in target.rglob("*") if p.is_file())
    return files


def _node_test_sites() -> list[tuple[str, int, str]]:
    sites: list[tuple[str, int, str]] = []
    for path in _candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _INVOCATION.search(line) and _IS_REAL_CALL.search(line):
                sites.append((str(path.relative_to(REPO_ROOT)), lineno, line.strip()))
    return sites


def test_the_scan_finds_the_known_invocations() -> None:
    """A zero is both a value and an error signature, so pin a floor.

    Without this, deleting the search paths or breaking the regex would make
    the guard below pass vacuously over an empty list.
    """
    sites = _node_test_sites()
    assert len(sites) >= 5, (
        "expected at least the five known `node --test` call sites; finding fewer "
        f"means the scan broke, not that the tree is clean (found {len(sites)}): {sites}"
    )


def test_every_node_test_invocation_uses_the_tap_reporter() -> None:
    offenders = [s for s in _node_test_sites() if "--test-reporter=tap" not in s[2]]
    assert not offenders, (
        "these `node --test` call sites still use the default spec reporter, which "
        "dies with RangeError while printing a large cancellation cascade and hides "
        "the failure entirely:\n"
        + "\n".join(f"  {p}:{n}: {line}" for p, n, line in offenders)
    )
