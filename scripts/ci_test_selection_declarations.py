"""PR-body CI-Tests declarations and bundle records for test selection."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from scripts.ci_test_budget import DECLARED_DISTANCE
from scripts.ci_test_selection_event import BUNDLE_VARIABLE, Event
from scripts.ci_test_selection_rules import FullSuite, Selection, lane_universe


#: `CI-Tests: tests/x/test_y.py -- why this PR needs it`, one module per line, at line start.
_DECLARATION_LINE = re.compile(r"^[ \t]*CI-Tests:(?P<rest>.*)$", re.M)
_DECLARATION = re.compile(r"^[ \t]*(?P<path>\S+)[ \t]+--[ \t]+\S")


def parse_declarations(body: str) -> tuple[list[str], list[str]]:
    """(declared module paths, errors) from a PR body's `CI-Tests:` lines.

    A line that is not `CI-Tests: <path> -- <reason>` is an error, never ignored and never
    half-read (Sol, PR #7598): the guard fails a record that carries one.
    """
    paths: list[str] = []
    errors: list[str] = []
    for match in _DECLARATION_LINE.finditer(body.replace("\r\n", "\n").replace("\r", "\n")):
        parsed = _DECLARATION.match(match["rest"])
        if parsed:
            paths.append(parsed["path"])
        else:
            errors.append(f"malformed `CI-Tests:{match['rest']}`, expected `CI-Tests: <path> -- <reason>`")
    return list(dict.fromkeys(paths)), errors


def declared_tests(body: str) -> list[str]:
    """The well-formed `CI-Tests:` module paths of a PR body, in order, deduplicated."""
    return parse_declarations(body)[0]


#: Test roots and suffixes of the non-pytest (frontend) suites; the frontend job runs them regardless.
FRONTEND_TEST_ROOT = "apps/webui/frontend/tests/"
FRONTEND_TEST_SUFFIXES = (".test.mjs", ".test.js", ".test.ts", ".spec.ts")


def _is_pytest_target(path: str) -> bool:
    """True for `tests/**/*.py` or a `::` node id under it."""
    return path.startswith("tests/") and path.split("::", 1)[0].endswith(".py")


def split_frontend_declarations(declared: list[str], root: Path) -> tuple[list[str], list[str]]:
    """(pytest-side declarations, existing frontend test paths): a frontend test is accepted, not refused.

    Only an existing path that is not a pytest target and sits under the frontend test root or
    carries a frontend test suffix moves to the second list; a missing path stays in the first, so
    `undeclarable` refuses it exactly as before.
    """
    pytest_side: list[str] = []
    frontend: list[str] = []
    for path in declared:
        is_frontend_name = path.startswith(FRONTEND_TEST_ROOT) or path.endswith(FRONTEND_TEST_SUFFIXES)
        if not _is_pytest_target(path) and is_frontend_name and (root / path).is_file():
            frontend.append(path)
        else:
            pytest_side.append(path)
    return pytest_side, frontend


def split_lane_ignored_declarations(
    declared: list[str], ignores: list[str], root: Path
) -> tuple[list[str], list[tuple[str, str]]]:
    """(declarations the lane may run or refuse, [(existing lane-ignored test, the ignore it sits under)]).

    A test that exists but is lane-ignored runs in another step (the reqs-check native step, say),
    so naming it is accepted and ignored for pytest selection, never refused. A missing path stays
    in the first list, so `undeclarable` refuses it exactly as before.
    """
    kept: list[str] = []
    ignored: list[tuple[str, str]] = []
    for path in declared:
        exists = bool(lane_universe([path], [], root))
        matched = next((ignore for ignore in ignores if exists and not lane_universe([path], [ignore], root)), None)
        if matched is not None:
            ignored.append((path, matched))
        else:
            kept.append(path)
    return kept, ignored


def undeclarable(paths: list[str], ignores: list[str], root: Path) -> list[str]:
    """Declared paths this lane never runs: not a test module, absent, or lane-ignored.

    The full lane carries the same ignores, so such a test would never run anywhere (Sol, PR
    #7598); it is an error the guard fails, whatever reason the body gives.
    """
    return [path for path in paths if not lane_universe([path], ignores, root)]


def bundle_record_problem(bundle: object) -> str | None:
    """Why a bundle record is malformed, or None; the selector and the guard share this check.

    An `unknown` record is well formed (it says why it could not read). A readable bundle needs
    unique positive PR numbers, `reasons` mapping some of those PRs to text, and string lists
    for `declared` and `errors` (Sol, PR #7598).
    """
    if not isinstance(bundle, dict) or not isinstance(bundle.get("bundle"), bool):
        return f"{bundle!r} is not a bundle record"
    if not bundle["bundle"] or bundle.get("unknown"):
        return None
    prs, reasons = bundle.get("prs"), bundle.get("reasons")
    if not isinstance(prs, list) or not prs or not all(type(pr) is int and pr > 0 for pr in prs):
        return f"prs is {prs!r}, not a non-empty list of PR numbers"
    if len(set(prs)) != len(prs):
        return f"prs {prs!r} repeats a PR"
    if not isinstance(reasons, dict) or not all(
        isinstance(pr, str) and pr.isdigit() and int(pr) in prs and isinstance(text, str) and text
        for pr, text in reasons.items()
    ):
        return f"reasons is {reasons!r}, not a mapping of the bundle's PR numbers to reason text"
    for field in ("declared", "errors"):
        values = bundle.get(field)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            return f"{field} is {values!r}, not a list of strings"
    return None


def bundle_declarations(bundle_json: str | None) -> tuple[list[str], list[int], list[str]] | None:
    """(declared modules, PRs, declaration errors) from a queue draft's bundle record.

    None when the record says this is not a bundle. An empty, unparseable or `unknown` record
    is FullSuite: the bundle cannot say what its PRs declared, and running more is the safe
    direction.
    """
    try:
        bundle = json.loads(bundle_json or "")
    except json.JSONDecodeError as error:
        raise FullSuite(
            f"{BUNDLE_VARIABLE} is not a bundle record ({error}); the bundle's declarations are unknown"
        ) from error
    problem = bundle_record_problem(bundle)
    if problem:
        raise FullSuite(f"the bundle record is malformed ({problem}); its declarations are unknown")
    if not bundle["bundle"]:
        return None
    if bundle.get("unknown"):
        raise FullSuite(f"the bundle's PR declarations could not be read: {bundle['unknown']}")
    return list(bundle["declared"]), list(bundle["prs"]), list(bundle["errors"])


def main_push_range(range_json: str | None) -> dict:
    """The scope job's CI_MAIN_PUSH_RANGE record (scripts/ci_main_push_range.py), `{}` when unreadable.

    Never FullSuite: a main push whose range is unknown tests HEAD's own change instead
    (`main_push_base`), because merged PR bodies are not something main can fix.
    """
    try:
        record = json.loads(range_json or "")
    except json.JSONDecodeError:
        return {}
    return record if isinstance(record, dict) and record.get("main_push") is True else {}


def run_declarations(
    event: Event, bundle_json: str | None, range_record: dict | None = None
) -> tuple[list[str], list[int], list[str]]:
    """(declared modules, bundle PRs, declaration errors) for this run; FullSuite when unreadable.

    A main push's declarations are its merged PRs' (`range_record`), and their errors are
    returned empty: a merged body cannot be fixed, so `decide` reports and drops them instead.
    """
    if event.main_push:
        record = range_record or {}
        return list(record.get("declared") or []), list(record.get("prs") or []), []
    if not event.queue_draft:
        paths, errors = parse_declarations(event.body)
        return paths, [], errors
    bundle = bundle_declarations(bundle_json)
    if bundle is None:
        raise FullSuite("this is a Trunk queue draft, but the scope job's bundle record says it is not")
    return bundle


def add_declared(selection: Selection, declared: list[str]) -> None:
    """Add the declared modules, already validated by `undeclarable`, to the selection."""
    for path in declared:
        selection.add(path, "declared in the PR body (`CI-Tests:`)", DECLARED_DISTANCE)
    selection.modules = sorted(selection.reasons)
