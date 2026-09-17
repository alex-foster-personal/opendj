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
# read is a failure instead of a silent skip (Sol P1 on PR #3468). The original
# version swallowed UnicodeDecodeError and OSError and carried on, which means
# an unreadable executable surface read exactly like a clean one -- the same
# unmeasured-as-green defect this guard exists to prevent. Binary artifacts
# genuinely live under these roots (.pyc, .m4a), so they are excluded BY NAME
# here rather than by catching an exception and hoping.
TEXT_SUFFIXES = frozenset({".py", ".sh", ".yml", ".yaml", ".json", ".mjs", ".mk", ".ts"})
EXTENSIONLESS_IN_SCOPE = frozenset({"Makefile", "justfile"})

# An invocation may be wrapped across lines -- in package.json a `node` and the
# `--test` that makes it a runner call can sit on adjacent lines -- so the scan
# runs over the WHOLE text rather than line by line (Sol P2 on PR #3468).
#
# `node` is matched WITHOUT a leading \b: the Makefile calls `$(FRONTEND_NODE)`,
# where the preceding `_` is a word character, so a word boundary there skips the
# site entirely. That is the same silent-skip class as the P1 above, and it is
# why IGNORECASE alone is not enough.
#
# The `--test` half is a LOOKAHEAD so the match consumes only `node`. A consuming
# regex swallows the following site's `node` inside its own match, and finditer
# never offers it again -- one real call site disappears for every false one
# found. Every `node` token therefore gets its own candidacy.
_INVOCATION = re.compile(r"node(?=[\s\S]{0,80}?--test\b)", re.IGNORECASE)
_WINDOW = 300
# A candidate is a real runner call, not prose or a neighbor's overspill, when
# its own window names a test file or a runner flag.
_IS_REAL_CALL = re.compile(r"\.test\.mjs|--test-concurrency")


def _in_scope(path: Path) -> bool:
    return path.suffix in TEXT_SUFFIXES or path.name in EXTENSIONLESS_IN_SCOPE


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
            "An executable surface that cannot be read is unmeasured, not clean; either "
            "make it readable or exclude it from TEXT_SUFFIXES deliberately."
        ) from exc


def _node_test_sites() -> list[tuple[str, int, str]]:
    sites: list[tuple[str, int, str]] = []
    for path in _candidate_files():
        text = _read_or_fail(path)
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
            sites.append((_display(path), lineno, window))
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
    by swallowing an exception."""
    for suffix in (".pyc", ".m4a"):
        assert suffix not in TEXT_SUFFIXES, f"{suffix} is binary and cannot be scanned as text"
    scanned = _candidate_files()
    assert scanned, "scope resolved to zero files"
    assert not [p for p in scanned if p.suffix in {".pyc", ".m4a"}]


def test_every_node_test_invocation_uses_the_tap_reporter() -> None:
    offenders = [s for s in _node_test_sites() if "--test-reporter=tap" not in s[2]]
    assert not offenders, (
        "these `node --test` call sites still use the default spec reporter, which "
        "dies with RangeError while printing a large cancellation cascade and hides "
        "the failure entirely:\n"
        + "\n".join(f"  {p}:{n}: {line}" for p, n, line in offenders)
    )
