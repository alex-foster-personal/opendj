"""scripts/suite_census.py and scripts/pytest_child_coverage.py.

The control is a real 4-test suite run under real coverage in a subprocess: a superset test, a
subset of it, a unique test, a test whose only coverage comes from a child Python process,
and a test whose child only re-imports the package (import bodies must not count as coverage).
Each guard is mutated in both directions: dropping the child-labelling plugin must make the
subprocess test unmeasured, and the set cover must neither keep a strict subset nor drop a
test that holds a unique arc.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import pytest_child_coverage as child
from scripts import suite_census as census

REPO_ROOT = Path(__file__).resolve().parents[2]

PKG = """
def f(x):
    if x > 0:
        return "pos"
    return "neg"


def g(x):
    return x * 2


def h(x):
    return x - 1
"""

CONTROL_TESTS = """
import subprocess, sys
from pkg import f, g


def test_a_superset():
    assert f(1) == "pos" and f(-1) == "neg"


def test_b_subset_of_a():
    assert f(1) == "pos"


def test_c_unique():
    assert g(2) == 4


def test_d_subprocess_only():
    out = subprocess.run([sys.executable, "-c", "import pkg; print(pkg.h(11))"], capture_output=True, text=True)
    assert out.stdout.strip() == "10"


def test_e_child_only_imports():
    out = subprocess.run([sys.executable, "-c", "import pkg"], capture_output=True, text=True)
    assert out.returncode == 0
"""

EXPECTED = {
    "test_a_superset": "KEEP_COVER",
    "test_b_subset_of_a": "REDUNDANT_IN_SET",
    "test_c_unique": "KEEP_COVER",
    "test_d_subprocess_only": "KEEP_COVER",
    # its child ran only module bodies the parent ran at import time: credited to no test
    "test_e_child_only_imports": "NO_COVERAGE",
}


def _run_control(tmp_path: Path, *, label_children: bool) -> tuple[Path, Path]:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text(PKG, encoding="utf-8")
    (tmp_path / "test_ctrl.py").write_text(CONTROL_TESTS, encoding="utf-8")
    rc = tmp_path / "coveragerc"
    rc.write_text(
        f"[run]\nbranch = true\nparallel = true\npatch = subprocess\nsource = pkg\ndata_file = {tmp_path / '.coverage'}\n",
        encoding="utf-8",
    )
    plugin = ["-p", "scripts.pytest_child_coverage"] if label_children else []
    env = {**os.environ, "PYTHONPATH": f"{tmp_path}{os.pathsep}{REPO_ROOT}"}
    env.pop("COVERAGE_PROCESS_CONFIG", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "test_ctrl.py",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            *plugin,
            "--cov",
            f"--cov-config={rc}",
            "--cov-context=test",
            "--cov-report=",
            f"--junitxml={tmp_path / 'junit.xml'}",
            "-o",
            "junit_family=xunit1",
            "-o",
            "addopts=",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return tmp_path / ".coverage", tmp_path / "junit.xml"


def _buckets(result: census.Census) -> dict[str, str]:
    return {row["test"].split("::")[1]: row["bucket"] for row in result.rows}


# ----- end-to-end control


def test_the_control_suite_buckets_every_test_correctly(tmp_path: Path) -> None:
    db, junit = _run_control(tmp_path, label_children=True)
    result = census.analyze(db, junit)
    assert _buckets(result) == EXPECTED
    assert result.child_contexts == 2
    assert result.cases == 5


def test_without_the_child_plugin_the_subprocess_test_is_unmeasured(tmp_path: Path) -> None:
    db, junit = _run_control(tmp_path, label_children=False)
    result = census.analyze(db, junit)
    assert _buckets(result)["test_d_subprocess_only"] == "NO_COVERAGE"
    assert result.child_contexts == 0


def test_a_coverage_context_naming_no_junit_test_is_unknown(tmp_path: Path) -> None:
    db, junit = _run_control(tmp_path, label_children=True)
    text = junit.read_text(encoding="utf-8")
    junit.write_text(text.replace('name="test_c_unique"', 'name="test_renamed"'), encoding="utf-8")
    with pytest.raises(census.CensusUnknown, match="name no junit test"):
        census.analyze(db, junit)


def test_the_cli_reports_unknown_with_exit_3_for_a_db_without_test_contexts(tmp_path: Path) -> None:
    db, junit = _run_control(tmp_path, label_children=True)
    empty = tmp_path / "empty.db"
    no_contexts = f"import coverage; c = coverage.Coverage(data_file={str(empty)!r}); c.start(); c.stop(); c.save()"
    subprocess.run([sys.executable, "-c", no_contexts], check=True)
    assert (
        census.main(["analyze", "--db", str(empty), "--junit", str(junit), "--out-dir", str(tmp_path / "out")])
        == census.EXIT_UNKNOWN
    )
    assert census.main(["analyze", "--db", str(db), "--junit", str(junit), "--out-dir", str(tmp_path / "out")]) == 0
    summary = json.loads((tmp_path / "out" / "census.json").read_text(encoding="utf-8"))
    assert summary["buckets"]["REDUNDANT_IN_SET"]["cases"] == 1


# ----- set cover, both directions


def test_the_cover_drops_a_strict_subset_even_when_it_is_the_cheapest_test() -> None:
    arcs = {"a": {(1, 1, 2), (1, 2, 3)}, "b": {(1, 1, 2)}, "c": {(1, 9, 10)}}
    assert census.minimal_cover(arcs, {"a": 5.0, "b": 0.0, "c": 1.0}) == {"a", "c"}


def test_the_cover_keeps_every_test_that_holds_a_unique_arc() -> None:
    arcs = {"a": {(1, 1, 2)}, "b": {(1, 2, 3)}, "c": {(1, 3, 4)}}
    assert census.minimal_cover(arcs, {"a": 9.0, "b": 9.0, "c": 9.0}) == {"a", "b", "c"}


# ----- child context rewrite


def test_the_child_config_is_relabelled_with_the_test_id() -> None:
    encoded = base64.b64encode(json.dumps({"context": None, "branch": True}).encode()).decode()
    decoded = json.loads(base64.b64decode(child.child_context_config(encoded, "t.py::test_x")))
    assert decoded == {"context": "t.py::test_x|subprocess", "branch": True}


def test_a_child_config_without_a_context_key_fails_loud() -> None:
    encoded = base64.b64encode(json.dumps({"branch": True}).encode()).decode()
    with pytest.raises(KeyError, match="no 'context' key"):
        child.child_context_config(encoded, "t.py::test_x")
