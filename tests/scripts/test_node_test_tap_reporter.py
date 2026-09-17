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

import pytest

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

# The scope is DECLARED rather than discovered, so that a file which cannot be
# read is a failure instead of a silent skip (Sol P1, first round). The original
# version swallowed UnicodeDecodeError and OSError and carried on, which means
# an unreadable executable surface read exactly like a clean one -- the same
# unmeasured-as-green defect this guard exists to prevent.
#
# The declaration is a DENY list, not an allow list (Sol P2, second round). An
# allow list of executable suffixes silently omits whatever nobody thought of --
# .js, .cjs, .bash today, something else tomorrow -- and a new `node --test` in
# one of those files would be invisible, leave the census unchanged, and never
# fail this guard. Defaulting to SCAN means a new format is covered the day it
# appears; a new BINARY format is a hard failure naming the file, which is the
# fail-closed direction and tells the reader exactly what to declare.
BINARY_SUFFIXES = frozenset({".pyc", ".m4a"})
# Prose is text, so it would scan cleanly, but a documented command is not a
# call site and counting one would put a false entry in the census.
PROSE_SUFFIXES = frozenset({".md", ".html", ".txt", ".example"})
UNSCANNED_SUFFIXES = BINARY_SUFFIXES | PROSE_SUFFIXES

# Node spells three different flags `--test*`: `--test`, `--test-reporter=` and
# `--test-concurrency=`. A plain `--test\b` matches the PREFIX of the other two,
# so `node --test-reporter=tap x.test.mjs` -- which no longer runs the test
# runner at all -- still read as a compliant site (Sol P1, second round). Require
# the flag to END: not followed by a dash, word character or `=`.
_STANDALONE_TEST = r"--test(?![-\w=])"

# An invocation may be wrapped across lines -- in package.json a `node` and the
# `--test` that makes it a runner call can sit on adjacent lines -- so the scan
# runs over the WHOLE text rather than line by line (Sol P2, first round).
#
# `node` is matched WITHOUT a leading \b: the Makefile calls `$(FRONTEND_NODE)`,
# where the preceding `_` is a word character, so a word boundary there skips the
# site entirely. That is the same silent-skip class as the P1s, and it is why
# IGNORECASE alone is not enough.
#
# The flag half is a LOOKAHEAD so the match consumes only `node`. A consuming
# regex swallows the following site's `node` inside its own match, and finditer
# never offers it again -- one real call site disappears for every false one
# found. Every `node` token therefore gets its own candidacy.
_INVOCATION = re.compile(rf"node(?=[\s\S]{{0,80}}?{_STANDALONE_TEST})", re.IGNORECASE)
_WINDOW = 300
# A candidate is a real runner call, not prose or a neighbor's overspill, when
# its own window names a test file or a runner flag.
_IS_REAL_CALL = re.compile(r"\.test\.mjs|--test-concurrency")


def _in_scope(path: Path) -> bool:
    return path.suffix not in UNSCANNED_SUFFIXES


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    for entry in SEARCH_PATHS:
        target = REPO_ROOT / entry
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(p for p in target.rglob("*") if p.is_file() and _in_scope(p))
    return files


def _display(path: Path) -> str:
    """Never let the reporting path raise: a file outside the repo (a test
    fixture) has no relative form, and a ValueError there would replace the
    message below with an unrelated traceback."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _read_or_fail(path: Path) -> str:
    """No except-and-continue: an unreadable surface is UNMEASURED, not clean."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        raise AssertionError(
            f"cannot scan {_display(path)} ({exc.__class__.__name__}: {exc}). "
            "An executable surface that cannot be read is unmeasured, not clean. If this "
            "is a new binary format, add its suffix to BINARY_SUFFIXES deliberately; do not "
            "restore a silent skip."
        ) from exc


def _scan_text(label: str, text: str) -> list[tuple[str, int, str]]:
    """The scan itself, over one blob. Split out so the guards below can be run
    against a crafted command instead of only against whatever the tree happens
    to contain today -- a test whose only fixture is the repo can only ever
    confirm the status quo."""
    sites: list[tuple[str, int, str]] = []
    starts = [m.start() for m in _INVOCATION.finditer(text)]
    for index, start in enumerate(starts):
        # Stop this window at the NEXT candidate. Without that bound, a
        # `node` that is not a runner call reads the following line's real
        # invocation as its own evidence and is counted as a compliant site.
        next_start = starts[index + 1] if index + 1 < len(starts) else len(text)
        window = text[start : min(start + _WINDOW, next_start)]
        if not _IS_REAL_CALL.search(window):
            continue
        lineno = text.count("\n", 0, start) + 1
        sites.append((label, lineno, window))
    return sites


def _node_test_sites() -> list[tuple[str, int, str]]:
    sites: list[tuple[str, int, str]] = []
    for path in _candidate_files():
        sites.extend(_scan_text(_display(path), _read_or_fail(path)))
    return sites


# The census as measured on this tree. A floor ("at least five") is too weak to
# be a control: it stays green when the scan LOSES a site and gains a spurious
# one, which is exactly what happened while fixing Sol P2 -- the Makefile call
# vanished behind a word boundary and a non-runner `node` in package.json read
# the next line's flags as its own. Per-file counts catch both directions.
EXPECTED_SITES_PER_FILE = {
    "Makefile": 1,
    "apps/webui/frontend/package.json": 4,
    "scripts/savepoint_gate.py": 1,
}


def test_the_scan_finds_the_known_invocations() -> None:
    """A zero is both a value and an error signature, so pin the census.

    Without this, deleting the search paths or breaking the regex would make
    the guard below pass vacuously over an empty list.
    """
    counts: dict[str, int] = {}
    for path, _lineno, _window in _node_test_sites():
        counts[path] = counts.get(path, 0) + 1
    assert counts == EXPECTED_SITES_PER_FILE, (
        "the `node --test` census moved. Finding FEWER means the scan broke, not "
        "that the tree is clean; finding MORE may be a real new call site or a "
        "non-runner `node` miscounted. Identify which before editing this number:\n"
        f"  expected {EXPECTED_SITES_PER_FILE}\n  measured {counts}"
    )


def test_a_site_carries_its_own_evidence_not_the_next_ones() -> None:
    """Each window must stand alone; overspill is how a false site is born."""
    for path, lineno, window in _node_test_sites():
        assert "--test" in window, f"{path}:{lineno} matched without evidence in its own window"
        assert window.count("--test-reporter=tap") <= 1, (
            f"{path}:{lineno} window spans more than one invocation, so this site's "
            f"compliance may be its neighbor's: {window!r}"
        )
        # Counting sites is not enough: a scan that consumes across the gap keeps
        # the COUNT right while reporting the wrong line, so a plain `node` is
        # credited with the next line's compliance. The `node` that opens the
        # window must be the one the flags belong to -- nothing between it and
        # its `--test` may be another `node`.
        head = window[: window.lower().index("--test")]
        assert "node" not in head.lower()[len("node") :], (
            f"{path}:{lineno} is not the invocation its own flags belong to; another "
            f"`node` sits between this match and its `--test`: {head!r}"
        )


def test_a_reporter_only_command_is_not_counted_as_a_test_runner_call() -> None:
    """`--test` is a PREFIX of `--test-reporter` and `--test-concurrency`, so a
    `--test\\b` match accepted a command that no longer runs the test runner at
    all and reported it as compliant (Sol P1, second round)."""
    runner = 'node --test --test-reporter=tap tests/unit/x.test.mjs'
    reporter_only = 'node --test-reporter=tap tests/unit/x.test.mjs'
    concurrency_only = 'node --test-concurrency=4 tests/unit/x.test.mjs'

    assert len(_scan_text("fixture", runner)) == 1, "a real runner call must still be found"
    for text in (reporter_only, concurrency_only):
        assert _scan_text("fixture", text) == [], (
            f"{text!r} has no standalone --test flag, so node does not run the test "
            "runner; counting it certifies a command that cannot fail this guard"
        )


def test_an_unreadable_surface_fails_instead_of_being_skipped(tmp_path: Path) -> None:
    """Sol P1: the old `except (UnicodeDecodeError, OSError): continue` made an
    unscannable file read exactly like a compliant one. Prove it now raises."""
    binary = tmp_path / "unreadable.py"
    binary.write_bytes(b"node --test \xff\xfe not utf-8")
    with pytest.raises(AssertionError, match="cannot scan"):
        _read_or_fail(binary)


def test_declared_scope_excludes_the_binaries_that_actually_live_here() -> None:
    """Fail-closed must not reject legitimate absence: the repo really does carry
    .pyc and .m4a under these roots, so they are out of scope BY NAME rather than
    by swallowing an exception. Everything not named is scanned."""
    for suffix in (".pyc", ".m4a"):
        assert suffix in BINARY_SUFFIXES, f"{suffix} is binary and cannot be scanned as text"
    scanned = _candidate_files()
    assert scanned, "scope resolved to zero files"
    assert not [p for p in scanned if p.suffix in UNSCANNED_SUFFIXES]
    # A format nobody listed must be SCANNED, never quietly skipped, or a new
    # call site could land in it and leave the census unmoved.
    for suffix in (".js", ".cjs", ".bash", ".ps1", ".cmd"):
        assert _in_scope(Path(f"scripts/whatever{suffix}")), (
            f"{suffix} is not scanned, so a `node --test` in one would be invisible"
        )


def test_every_node_test_invocation_uses_the_tap_reporter() -> None:
    offenders = [s for s in _node_test_sites() if "--test-reporter=tap" not in s[2]]
    assert not offenders, (
        "these `node --test` call sites still use the default spec reporter, which "
        "dies with RangeError while printing a large cancellation cascade and hides "
        "the failure entirely:\n"
        + "\n".join(f"  {p}:{n}: {line}" for p, n, line in offenders)
    )
