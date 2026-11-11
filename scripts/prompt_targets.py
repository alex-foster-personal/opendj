"""Validate a TEST disposition's pytest target by running it (REQHYG-03, issue #11334)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

EXIT_UNAVAILABLE = 3
_NO_TESTS_COLLECTED = 5
_NONE_PR_RE = re.compile(r"(?i)none\s*\(PR\s*#\d+\)")
_SKIP_LINE = re.compile(
    r"^SKIPPED(?:\s+\[\d+\])?\s+(?P<loc>\S+?):\s*(?P<reason>.*)$",
    re.MULTILINE,
)
_COLLECT_PLUGIN = Path(__file__).with_name("pytest_disposition_collect.py")


def _pytest_target_file(target: str) -> str:
    return target.split("::", 1)[0]


def is_pytest_target(target: str) -> bool:
    file_part = _pytest_target_file(target)
    return file_part.endswith(".py") and "::" in target


def _skip_summary_matches(target: str, loc: str) -> bool:
    """`loc` is pytest's ``file:line`` summary locator for the skipped item."""
    file_part = _pytest_target_file(target)
    return loc == target or loc.startswith(file_part)


def _run_pytest_node(checkout: Path, target: str) -> tuple[str, str]:
    """Outcome of a real run: passed, skipped, failed, missing, unavailable."""
    test_file = _pytest_target_file(target)
    candidate = (checkout / test_file).resolve()
    if not candidate.is_relative_to(checkout.resolve()) or not candidate.is_file():
        return "missing", f"unknown pytest node id: {target} (collected in {checkout})"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            target,
            "-p",
            "no:randomly",
            "-rs",
            "-q",
        ],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    if result.returncode == _NO_TESTS_COLLECTED:
        return "missing", f"unknown pytest node id: {target} (collected in {checkout})"
    if result.returncode not in (0, 1):
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        return "unavailable", detail
    passed = re.search(rf"^PASSED\s+{re.escape(target)}\b", combined, re.MULTILINE)
    for match in _SKIP_LINE.finditer(combined):
        if _skip_summary_matches(target, match.group("loc")):
            reason = match.group("reason").strip() or "skipped"
            return "skipped", reason
    if not passed and re.search(r"=\s*s+\s+\[100%\]", combined):
        return "skipped", "skipped"
    if result.returncode == 1:
        return "failed", (result.stdout or result.stderr).strip() or "pytest reported failures"
    return "passed", ""


def pytest_run_refusal(target: str, kind: str, detail: str) -> str:
    """Human refusal for a node a run did not execute or pass."""
    if kind == "skipped":
        return (
            f"skipped test: {target} was SKIPPED during the validation run ({detail}); "
            "cite a test that runs or use NOTE"
        )
    if kind == "missing":
        return detail
    if kind == "failed":
        return (
            f"failing test: {target} did not pass under pytest validation ({detail}); "
            "fix the test or cite one that passes"
        )
    return f"UNAVAILABLE: pytest validation for {target}: {detail}"


def _requirement_problem(
    target: str,
    marker_ids: tuple[str, ...],
    known: frozenset[str],
    has_intent: bool,
) -> str | None:
    def cites(rid: str) -> bool:
        return rid in known or bool(_NONE_PR_RE.fullmatch(rid))

    if not marker_ids:
        return (
            f"test carries no requirement marker: {target}; "
            'mark it @pytest.mark.requirement("<ID>") (REQHYG-02), or cite a marked test'
        )
    unknown = [i for i in marker_ids if not cites(i)]
    if unknown:
        return (
            f"test's requirement marker cites a token that is not an id in reqs.json: {target} "
            f"cites {', '.join(repr(i) for i in unknown)}; cite a registered id or use NOTE"
        )
    if not has_intent:
        return (
            f"test carries no `[if] ... [then] ..., [else stop]` intent line for its marker: "
            f"{target}; put it in the test's own docstring (REQHYG-03)"
        )
    return None


def _collect_marker_ids(checkout: Path, target: str) -> tuple[tuple[str, ...], bool]:
    """Requirement marker ids and intent docstring for `target` (collection metadata only)."""
    test_file = _pytest_target_file(target)
    with tempfile.TemporaryDirectory(prefix="reqhyg03-collect-") as tmp:
        out = Path(tmp) / "marks.json"
        shutil.copy(_COLLECT_PLUGIN, Path(tmp) / "pytest_disposition_collect.py")
        path = os.pathsep.join(p for p in (tmp, os.environ.get("PYTHONPATH", "")) if p)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-p",
                "no:randomly",
                "-p",
                "pytest_disposition_collect",
                f"--rootdir={checkout}",
                test_file,
            ],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONPATH": path, "REQHYG03_MARKS_OUT": str(out), "REQHYG03_TARGET": target},
        )
        if proc.returncode not in (0, _NO_TESTS_COLLECTED) or not out.is_file():
            return (), False
        data = json.loads(out.read_text(encoding="utf-8"))
        if not data.get("found"):
            return (), False
        return tuple(data.get("ids", [])), bool(data.get("has_intent"))


def target_test_problem(checkout: Path, target: str, known: Callable[[], frozenset[str]]) -> str | None:
    """Why a TEST target is not a real, requirement-marked test a run executes, else None."""
    if not is_pytest_target(target):
        return f"unknown pytest node id: {target} (expected path/to/test.py::node)"
    kind, detail = _run_pytest_node(checkout, target)
    if kind == "unavailable":
        print(pytest_run_refusal(target, kind, detail), file=sys.stderr)
        sys.exit(EXIT_UNAVAILABLE)
    if kind != "passed":
        return pytest_run_refusal(target, kind, detail)
    marker_ids, has_intent = _collect_marker_ids(checkout, target)
    return _requirement_problem(target, marker_ids, known(), has_intent)


def collect_only_lists_node(checkout: Path, target: str) -> bool:
    """Whether ``pytest --collect-only`` would list `target` (not proof it runs)."""
    test_file = _pytest_target_file(target)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:randomly",
            test_file,
        ],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, _NO_TESTS_COLLECTED):
        return False
    nodes = {line.strip() for line in result.stdout.splitlines() if "::" in line}
    return target in nodes or any(n.startswith(target + "[") for n in nodes)
