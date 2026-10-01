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


def k(x):
    return x + 7


def m(x):
    return x * 3
"""

CONTROL_TESTS = """
import subprocess, sys
import pytest
from pkg import f, g, m


@pytest.fixture(scope="module")
def child_ran_k():
    out = subprocess.run([sys.executable, "-c", "import pkg; print(pkg.k(1))"], capture_output=True, text=True)
    return out.stdout.strip()


def test_f_uses_a_module_fixture_child(child_ran_k):
    assert child_ran_k == "8"


class TestPair:
    def test_same(self):
        assert f(1) == "pos"


class TestOther:
    def test_same(self):
        assert m(4) == 12


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
    # its only coverage is a child spawned by a MODULE-scoped fixture (Sol P1, #4777)
    "test_f_uses_a_module_fixture_child": "KEEP_COVER",
    # same method name in two classes: both keep their own identity (Sol P2, #4777)
    "TestPair::test_same": "REDUNDANT_IN_SET",
    "TestOther::test_same": "KEEP_COVER",
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
    env = {
        **os.environ,
        "PYTHONPATH": f"{tmp_path}{os.pathsep}{REPO_ROOT}",
        child.CORE_DIR_ENV: str(tmp_path / "cores"),
    }
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
    return {row["test"].split("::", 1)[1]: row["bucket"] for row in result.rows}


# ----- end-to-end control


@pytest.mark.requirement("DEVOPS-21")
def test_the_control_suite_buckets_every_test_correctly(tmp_path: Path) -> None:
    """[if] a test's only coverage comes from a child Python process [then] it is credited to that test, [else stop].

    [if] a child only re-imports the package [then] that test stays NO_COVERAGE."""
    db, junit = _run_control(tmp_path, label_children=True)
    result = census.analyze(db, junit)
    assert _buckets(result) == EXPECTED
    assert result.child_contexts == 3
    assert result.cases == 8


def test_without_the_child_plugin_the_subprocess_test_is_unmeasured(tmp_path: Path) -> None:
    db, junit = _run_control(tmp_path, label_children=False)
    result = census.analyze(db, junit)
    assert _buckets(result)["test_d_subprocess_only"] == "NO_COVERAGE"
    assert result.child_contexts == 0


@pytest.mark.requirement("DEVOPS-21")
def test_a_coverage_context_naming_no_junit_test_is_unknown(tmp_path: Path) -> None:
    """[if] a coverage context names no junit test [then] the census is UNKNOWN, [else stop]."""
    db, junit = _run_control(tmp_path, label_children=True)
    text = junit.read_text(encoding="utf-8")
    junit.write_text(text.replace('name="test_c_unique"', 'name="test_renamed"'), encoding="utf-8")
    with pytest.raises(census.CensusUnknown, match="name no junit test"):
        census.analyze(db, junit)


@pytest.mark.requirement("DEVOPS-21")
def test_the_cli_reports_unknown_with_exit_3_for_a_db_without_test_contexts(tmp_path: Path) -> None:
    """[if] the coverage data has no per-test contexts [then] the CLI exits 3 (UNKNOWN), [else stop]."""
    db, junit = _run_control(tmp_path, label_children=True)
    empty = tmp_path / "empty.db"
    no_contexts = f"import coverage; c = coverage.Coverage(data_file={str(empty)!r}); c.start(); c.stop(); c.save()"
    subprocess.run([sys.executable, "-c", no_contexts], check=True)
    assert (
        census.main(
            [
                "analyze",
                "--db",
                str(empty),
                "--junit",
                str(junit),
                "--cores-dir",
                str(tmp_path / "cores"),
                "--out-dir",
                str(tmp_path / "out"),
            ]
        )
        == census.EXIT_UNKNOWN
    )
    assert (
        census.main(
            [
                "analyze",
                "--db",
                str(db),
                "--junit",
                str(junit),
                "--cores-dir",
                str(tmp_path / "cores"),
                "--out-dir",
                str(tmp_path / "out"),
            ]
        )
        == 0
    )
    summary = json.loads((tmp_path / "out" / "census.json").read_text(encoding="utf-8"))
    assert summary["buckets"]["REDUNDANT_IN_SET"]["cases"] == 2  # test_b and TestPair::test_same


@pytest.mark.requirement("DEVOPS-21")
@pytest.mark.parametrize("bad_time", [None, "nan", "-1.0", "inf"])
def test_a_missing_or_invalid_junit_duration_is_unknown(tmp_path: Path, bad_time: str | None) -> None:
    """[if] a junit testcase has no finite nonnegative time [then] the census is UNKNOWN, [else stop].

    Sol P1 on #4777: a missing time used to become 0 s and feed the totals and the cover."""
    db, junit = _run_control(tmp_path, label_children=True)
    text = junit.read_text(encoding="utf-8")
    start = text.index('name="test_c_unique"')
    head, tail = text[:start], text[start:]
    time_attr = tail[tail.index(' time="') : tail.index('"', tail.index(' time="') + 7) + 1]
    replacement = "" if bad_time is None else f' time="{bad_time}"'
    junit.write_text(head + tail.replace(time_attr, replacement, 1), encoding="utf-8")
    with pytest.raises(census.CensusUnknown, match="time"):
        census.analyze(db, junit)


def test_a_zero_duration_is_a_measurement_not_a_gap(tmp_path: Path) -> None:
    """Control for the opposite overshoot: pytest writes time="0.000" for fast and skipped cases."""
    db, junit = _run_control(tmp_path, label_children=True)
    text = junit.read_text(encoding="utf-8")
    start = text.index('name="test_c_unique"')
    head, tail = text[:start], text[start:]
    time_attr = tail[tail.index(' time="') : tail.index('"', tail.index(' time="') + 7) + 1]
    junit.write_text(head + tail.replace(time_attr, ' time="0.000"', 1), encoding="utf-8")
    assert census.analyze(db, junit).cases == 8


def test_a_failed_census_leaves_no_earlier_verdict_behind(tmp_path: Path) -> None:
    """Sol P2 on #4777: a reused out-dir must not keep a previous run's census.json."""
    db, junit = _run_control(tmp_path, label_children=True)
    out = tmp_path / "out"
    assert (
        census.main(
            [
                "analyze",
                "--db",
                str(db),
                "--junit",
                str(junit),
                "--cores-dir",
                str(tmp_path / "cores"),
                "--out-dir",
                str(out),
            ]
        )
        == 0
    )
    assert (out / "census.json").exists() and (out / "rows.json").exists()
    junit.write_text(
        junit.read_text(encoding="utf-8").replace('name="test_c_unique"', 'name="renamed"'), encoding="utf-8"
    )
    assert (
        census.main(
            [
                "analyze",
                "--db",
                str(db),
                "--junit",
                str(junit),
                "--cores-dir",
                str(tmp_path / "cores"),
                "--out-dir",
                str(out),
            ]
        )
        == census.EXIT_UNKNOWN
    )
    assert not (out / "census.json").exists()
    assert not (out / "rows.json").exists()


def test_a_new_run_clears_leftover_coverage_fragments(tmp_path: Path) -> None:
    stale = tmp_path / ".coverage.crashed-host.123.abc"
    stale.write_text("stale", encoding="utf-8")
    (tmp_path / "census.json").write_text("{}", encoding="utf-8")
    census.clear_previous_outputs(tmp_path)
    assert not stale.exists()
    assert not (tmp_path / "census.json").exists()


@pytest.mark.requirement("DEVOPS-21")
def test_the_census_records_the_core_that_actually_ran(tmp_path: Path) -> None:
    """[if] the census measured a run [then] it proves each test process used a validated core, [else stop].

    Sol P1 on #4777: the sysmon core can drop a later test's context, so asking for ctrace is not
    enough; the core that really ran is recorded by the plugin and checked."""
    _run_control(tmp_path, label_children=True)
    cores = census.verify_cores(tmp_path / "cores")
    assert cores and set(cores) <= census.VALIDATED_CORES


@pytest.mark.parametrize("recorded", [None, "SysMonitor"])
def test_an_unvalidated_or_unrecorded_core_is_unknown(tmp_path: Path, recorded: str | None) -> None:
    cores = tmp_path / "cores"
    cores.mkdir()
    if recorded is not None:
        (cores / "123.core").write_text(recorded, encoding="utf-8")
    with pytest.raises(census.CensusUnknown, match="core"):
        census.verify_cores(cores)


# ----- set cover, both directions


@pytest.mark.requirement("DEVOPS-21")
def test_the_cover_drops_a_strict_subset_even_when_it_is_the_cheapest_test() -> None:
    """[if] a test is a strict coverage subset of another [then] it is REDUNDANT_IN_SET even when cheapest, [else stop]."""
    arcs = {"a": {(1, 1, 2), (1, 2, 3)}, "b": {(1, 1, 2)}, "c": {(1, 9, 10)}}
    assert census.minimal_cover(arcs, {"a": 5.0, "b": 0.0, "c": 1.0}) == {"a", "c"}


@pytest.mark.requirement("DEVOPS-21")
def test_a_strict_subset_is_dropped_even_when_its_superset_is_never_selected() -> None:
    """[if] a cheap test is a strict subset of an expensive one [then] it is not kept, [else stop].

    Sol P2 on #4777: a={1,2} expensive, b={1} and c={2,3} cheap. Greedy alone keeps b and c."""
    arcs = {"a": {(1, 1, 2), (1, 2, 3)}, "b": {(1, 1, 2)}, "c": {(1, 2, 3), (1, 3, 4)}}
    assert "b" not in census.minimal_cover(arcs, {"a": 9.0, "b": 0.0, "c": 0.0})


def test_of_two_identical_tests_exactly_one_is_kept() -> None:
    arcs = {"x": {(1, 1, 2)}, "y": {(1, 1, 2)}}
    assert len(census.minimal_cover(arcs, {"x": 1.0, "y": 1.0})) == 1


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
