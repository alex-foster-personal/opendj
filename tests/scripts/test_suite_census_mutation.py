"""scripts/suite_census_mutation.py: the mutation verdict on top of the suite census.

The control is a real throwaway git repo with a 6-test suite, measured by the real census under
real coverage, then scored by real mutants:

- ``test_strong`` and ``test_twin`` assert the same thing on the same arcs, so the census calls one
  of them REDUNDANT_IN_SET; both catch every mutant of ``f``, so the K=2 mutant cover keeps both.
  That is the case the census alone gets wrong.
- ``test_weak`` runs half of ``f`` and asserts nothing about it: a strict arc subset of the twins, so
  REDUNDANT_IN_SET whatever the timings, and it kills nothing: the one ``candidate``.
- ``test_g`` holds unique arcs (KEEP_COVER), ``test_nothing`` runs no measured code (unscored),
  ``test_fails`` fails in the census (keep, by outcome).

Each guard is checked in the other direction too: a budget stop or a dirty module turns the
candidate into ``unscored``, never into a candidate with no evidence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import pytest_child_coverage as child
from scripts import suite_census as census
from scripts import suite_census_mutation as mut

REPO_ROOT = Path(__file__).resolve().parents[2]

PKG = """
def f(x):
    if x > 0:
        return "pos"
    return "neg"


def g(x):
    return x * 2
"""

TESTS = """
from pkg import f, g


def test_strong():
    assert f(1) == "pos" and f(-1) == "neg"


def test_twin():
    assert f(1) == "pos" and f(-1) == "neg"


def test_weak():
    f(1)


def test_g():
    assert g(2) == 4


def test_nothing():
    assert 1 + 1 == 2


def test_fails():
    assert g(1) == 3
"""

# the mutant runs pass this repo's own option; a foreign repo's conftest has to accept it
CONFTEST = """
def pytest_addoption(parser):
    parser.addoption("--no-coverage-matrix", action="store_true")
"""

GIT = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "init.defaultBranch=main"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run([*GIT, *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _census(tmp_path: Path) -> tuple[Path, Path]:
    """A committed control repo and a census dir measured on it by the real census."""
    repo, out = tmp_path / "repo", tmp_path / "census"
    (repo / "pkg").mkdir(parents=True)
    out.mkdir()
    (repo / "pkg" / "__init__.py").write_text(PKG, encoding="utf-8")
    (repo / "test_ctrl.py").write_text(TESTS, encoding="utf-8")
    (repo / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "control")
    rc = tmp_path / "coveragerc"
    rc.write_text(
        "[run]\nbranch = true\nrelative_files = true\nparallel = true\npatch = subprocess\n"
        f"source = pkg\ndata_file = {out / '.coverage'}\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": f"{repo}{os.pathsep}{REPO_ROOT}", child.CORE_DIR_ENV: str(out / "cores")}
    env.pop(child.PROCESS_CONFIG_ENV, None)
    proc = subprocess.run(
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
            "-p",
            "scripts.pytest_child_coverage",
            "--cov",
            f"--cov-config={rc}",
            "--cov-context=test",
            "--cov-report=",
            f"--junitxml={out / 'junit.xml'}",
            "-o",
            "junit_family=xunit1",
            "-o",
            "addopts=",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr  # test_fails is the only failure
    args = ["analyze", "--db", str(out / ".coverage"), "--junit", str(out / "junit.xml")]
    assert census.main([*args, "--cores-dir", str(out / "cores"), "--out-dir", str(out)]) == 0
    return repo, out


def _main(repo: Path, out: Path, *extra: str) -> int:
    return mut.main(["--repo", str(repo), "--census-dir", str(out), "--budget-min", "5", "--prefix", "pkg/", *extra])


def _verdicts(out: Path) -> dict[str, str]:
    doc = json.loads((out / mut.VERDICT_FILE).read_text(encoding="utf-8"))
    return {t.split("::", 1)[1]: row["verdict"] for t, row in doc["tests"].items()}


@pytest.fixture(scope="module")
def scored(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, dict]:
    tmp = tmp_path_factory.mktemp("mutation")
    repo, out = _census(tmp)
    assert _main(repo, out) == 0
    return repo, out, json.loads((out / mut.VERDICT_FILE).read_text(encoding="utf-8"))


# ----- end to end


@pytest.mark.requirement("DEVOPS-5636")
def test_the_control_suite_gets_the_expected_verdicts(scored: tuple[Path, Path, dict]) -> None:
    """[if] a census-redundant test catches mutants no K=2 cover can spare [then] it is keep, [else stop].

    [if] a REDUNDANT_IN_SET test kills nothing and every module it reaches is scored [then] it is candidate."""
    _, out, doc = scored
    assert _verdicts(out) == {  # reasons: see doc["tests"]
        "test_strong": "keep",
        "test_twin": "keep",
        "test_weak": "candidate",
        "test_g": "keep",
        "test_nothing": "unscored",
        "test_fails": "keep",
    }
    buckets = {t.split("::", 1)[1]: row["bucket"] for t, row in doc["tests"].items()}
    # the census alone would have offered one of the twins for deletion
    assert sorted(buckets[t] for t in ("test_strong", "test_twin")) == ["KEEP_COVER", "REDUNDANT_IN_SET"]
    assert doc["modules_scored"] == 1 and doc["mutants_caught"] >= 1


def test_mutation_restores_the_module_byte_for_byte(scored: tuple[Path, Path, dict]) -> None:
    repo, _, _ = scored
    assert (repo / "pkg" / "__init__.py").read_text(encoding="utf-8") == PKG
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""


def test_scores_are_written_for_the_next_run_to_carry(scored: tuple[Path, Path, dict]) -> None:
    _, out, _ = scored
    score = json.loads((out / mut.SCORES_DIR / "pkg____init__.py.json").read_text(encoding="utf-8"))
    assert score["module"] == "pkg/__init__.py" and score["stopped_early"] is None
    assert score["mutants"] and all("killed_by" in m for m in score["mutants"])


@pytest.mark.requirement("DEVOPS-5636")
def test_a_fresh_carried_score_is_reused_not_rescored(scored: tuple[Path, Path, dict], tmp_path: Path) -> None:
    """[if] a previous score matches the module's source and is recent [then] it is carried, not re-run, [else stop]."""
    repo, out, _ = scored
    out2 = tmp_path / "again"
    out2.mkdir()
    for name in ("census.json", "rows.json", "junit.xml", ".coverage"):
        (out2 / name).write_bytes((out / name).read_bytes())
    assert _main(repo, out2, "--previous-dir", str(out / mut.SCORES_DIR)) == 0
    doc = json.loads((out2 / mut.VERDICT_FILE).read_text(encoding="utf-8"))
    assert doc["scored_this_run"] == [] and doc["carried_modules"] == ["pkg/__init__.py"]
    assert _verdicts(out2) == _verdicts(out)


# ----- the candidate verdict needs evidence: both directions


@pytest.mark.requirement("DEVOPS-5636")
def test_a_budget_stop_leaves_the_candidate_unscored(tmp_path: Path) -> None:
    """[if] the budget ends before a module is fully scored [then] its redundant tests are unscored, [else stop]."""
    repo, out = _census(tmp_path)
    rows, outcomes, reach = mut.load_census(out, repo)
    tests = sorted(t for t in reach["pkg/__init__.py"] if outcomes[t] == "passed")
    score = mut.score_module(repo, "pkg/__init__.py", tests, cap=40, deadline=time.monotonic(), per_run_timeout_s=120)
    assert score.stopped_early == "budget" and score.mutants == []
    doc = mut.build_verdicts(rows, outcomes, reach, {"pkg/__init__.py": score.__dict__})
    weak = next(row for t, row in doc["tests"].items() if t.endswith("::test_weak"))
    assert (weak["verdict"], weak["modules_unscored"]) == ("unscored", 1), weak


@pytest.mark.requirement("DEVOPS-5636")
def test_a_module_with_uncommitted_changes_is_never_mutated(tmp_path: Path) -> None:
    """[if] a module has uncommitted changes [then] it is skipped untouched and its tests stay unscored, [else stop]."""
    repo, out = _census(tmp_path)
    edited = PKG + "\n# local edit\n"
    (repo / "pkg" / "__init__.py").write_text(edited, encoding="utf-8")
    assert _main(repo, out) == 0
    doc = json.loads((out / mut.VERDICT_FILE).read_text(encoding="utf-8"))
    assert "pkg/__init__.py" in doc["skipped_modules"]
    assert (repo / "pkg" / "__init__.py").read_text(encoding="utf-8") == edited
    assert "candidate" not in _verdicts(out).values()


# ----- UNKNOWN, never a verdict


@pytest.mark.requirement("DEVOPS-5636")
def test_a_census_dir_without_census_json_is_unknown(tmp_path: Path) -> None:
    """[if] the census did not measure (no census.json) [then] the scorer exits 3 and writes no verdict, [else stop]."""
    repo, out = _census(tmp_path)
    (out / "census.json").unlink()
    (out / mut.VERDICT_FILE).write_text("{}", encoding="utf-8")
    assert _main(repo, out) == mut.EXIT_UNKNOWN
    assert not (out / mut.VERDICT_FILE).exists()


def test_a_census_of_another_commit_is_unknown(tmp_path: Path) -> None:
    repo, out = _census(tmp_path)
    summary = json.loads((out / "census.json").read_text(encoding="utf-8"))
    (out / "census.json").write_text(json.dumps(summary | {"sha": "0" * 40}), encoding="utf-8")
    assert _main(repo, out) == mut.EXIT_UNKNOWN
    summary["sha"] = _git(repo, "rev-parse", "HEAD")  # control: the right sha measures
    (out / "census.json").write_text(json.dumps(summary), encoding="utf-8")
    assert _main(repo, out, "--cap", "1") == 0


# ----- pure parts


@pytest.mark.parametrize(
    ("source", "kind", "needle"),
    [
        ("x = a == b\n", "cmp", "a != b"),
        ("x = a < b\n", "cmp", "a >= b"),
        ("x = a + b\n", "op", "a - b"),
        ("x = a and b\n", "op", "a or b"),
        ("x = True\n", "bool", "x = False"),
        ("x = 1\n", "int", "x = 2"),
        ("def f():\n    return 5\n", "ret", "return None"),
        ("if a:\n    pass\n", "notif", "if not a"),
    ],
)
def test_each_operator_plants_its_fault(source: str, kind: str, needle: str) -> None:
    site = next(s for s in mut.sites(source) if s.kind == kind)
    mutated = mut.apply(source, site)
    assert mutated is not None and needle in mutated


def test_a_site_that_is_not_there_plants_nothing() -> None:
    assert mut.apply("x = 3\n", mut.Site("int", 1, 4, "Constant")) is None


def test_mutant_choice_is_stable_per_module() -> None:
    src = PKG * 3
    assert mut.choose(src, "a.py", 5) == mut.choose(src, "a.py", 5)
    assert len(mut.choose(src, "a.py", 5)) == 5


def test_the_keep_cover_keeps_two_killers_where_two_exist() -> None:
    kills = [{"a", "b", "c"}, {"c"}]
    chosen = mut.keep_cover(["a", "b", "c"], kills, {"a": 1.0, "b": 9.0, "c": 1.0})
    assert chosen == {"a", "c"}  # c is the sole killer of mutant 2; a is the cheaper second killer


def test_a_crashed_mutant_is_not_evidence_for_any_test() -> None:
    rows = [{"test": "t.py::a", "bucket": "REDUNDANT_IN_SET", "arcs": 1, "unique_arcs": 0, "dur_s": 0.1}]
    score = {
        "module": "m.py",
        "tests": ["t.py::a"],
        "mutants": [{"id": "x", "killed_by": [mut.CRASH]}],
        "scored_at": "2026-10-06T00:00:00+0000",
        "source_sha256": "0",
        "stopped_early": None,
    }
    doc = mut.build_verdicts(rows, {"t.py::a": "passed"}, {"m.py": {"t.py::a"}}, {"m.py": score})
    assert doc["tests"]["t.py::a"]["verdict"] == "candidate"
    assert doc["modules"]["m.py"]["caught"] == 0 and doc["modules"]["m.py"]["crashed"] == 1
