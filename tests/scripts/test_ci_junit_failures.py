"""scripts/ci_junit_failures.py: a red shard names its failing test ids outside the job log."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import ci_junit_failures as mod

REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.scripts.test_a.TestThing" name="test_bad"><failure message="x"/></testcase>
  <testcase classname="tests.scripts.test_a" name="test_ok"/>
  <testcase classname="tests.scripts.test_a" name="test_err[p-1]"><error message="boom"/></testcase>
  <testcase classname="tests.scripts.test_a" name="test_skip"><skipped/></testcase>
  <testcase classname="tests.gone.test_removed" name="test_lost"><failure/></testcase>
  <testcase classname="tests.scripts.test_a.TestThing" name="test_bad"><error message="teardown"/></testcase>
  <testcase classname="" name="tests.scripts.test_a"><error message="collection failed"/></testcase>
</testsuite></testsuites>
"""


@pytest.fixture(autouse=True)
def _no_runner_env(monkeypatch) -> None:
    # Under CI these point at the real job's files; a test must never write there.
    for env in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "GITHUB_ACTIONS"):
        monkeypatch.delenv(env, raising=False)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "tests" / "scripts").mkdir(parents=True)
    (tmp_path / "tests" / "scripts" / "test_a.py").write_text("")
    (tmp_path / "junit.xml").write_text(REPORT)
    return tmp_path


def test_only_failures_and_errors_become_node_ids(tree: Path) -> None:
    assert mod.failing_ids(tree / "junit.xml", tree) == [
        "tests/scripts/test_a.py::TestThing::test_bad",
        "tests/scripts/test_a.py::test_err[p-1]",
        # No file resolves, so it is marked rather than passed off as a node id.
        "UNRESOLVED tests.gone.test_removed::test_lost",
        # A collection error names the module; pytest reproduces it by path.
        "tests/scripts/test_a.py",
    ]


def test_teardown_error_after_failure_is_one_id(tree: Path) -> None:
    ids = mod.failing_ids(tree / "junit.xml", tree)
    assert ids.count("tests/scripts/test_a.py::TestThing::test_bad") == 1


def test_collection_error_uses_file_attribute(tmp_path: Path) -> None:
    (tmp_path / "tests" / "y").mkdir(parents=True)
    (tmp_path / "tests" / "y" / "test_z.py").write_text("")
    assert mod.node_id("", "x", tmp_path, "tests/y/test_z.py") == "tests/y/test_z.py"
    assert mod.node_id("tests.y.test_z.TestA", "t", tmp_path, "tests/y/test_z.py") == "tests/y/test_z.py::TestA::t"
    assert mod.node_id("", "tests.nowhere", tmp_path) == "UNRESOLVED tests.nowhere"
    # A file attribute that is not in this checkout is not trusted either.
    assert mod.node_id("tests.gone.m", "t", tmp_path, "tests/gone/m.py") == "UNRESOLVED tests.gone.m::t"


def test_annotations_summary_and_output(tree: Path, monkeypatch, capsys) -> None:
    summary, output = tree / "summary.md", tree / "output"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    rc = mod.main([str(tree / "junit.xml"), "--label", "shard 2 of 5", "--root", str(tree)])
    assert rc == 0
    out = capsys.readouterr().out
    # Warnings, not errors: quarantine may still excuse these after this step.
    assert "\n::warning title=failing test (shard 2 of 5)::tests/scripts/test_a.py::TestThing::test_bad" in out
    assert out.count("::warning ") == 4
    assert "::error" not in out
    assert output.read_text() == "failing_count=4\n"
    text = summary.read_text()
    assert "### Failing tests (shard 2 of 5): 4" in text
    assert "Reproduce only these: `pytest tests/scripts/test_a.py::TestThing::test_bad " in text
    assert "'tests/scripts/test_a.py::test_err[p-1]'" in text


def test_default_annotation_cap_is_ten_and_summary_is_uncapped(tmp_path: Path, monkeypatch, capsys) -> None:
    cases = "".join(f'<testcase classname="m" name="t{i}"><failure/></testcase>' for i in range(250))
    (tmp_path / "many.xml").write_text(f"<testsuite>{cases}</testsuite>")
    (tmp_path / "m.py").write_text("")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    mod.main([str(tmp_path / "many.xml"), "--label", "s", "--root", str(tmp_path)])
    assert capsys.readouterr().out.count("::warning ") == 10
    text = summary.read_text()
    assert "m.py::t0\n" in text and "m.py::t249\n" in text
    assert "(first 5 of 250; the full list is above)" in text


def test_missing_targets_fail_under_actions(tree: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert mod.main([str(tree / "junit.xml"), "--label", "s", "--root", str(tree)]) == 1
    assert "::error title=failing tests not reported (s)::GITHUB_OUTPUT is not set" in capsys.readouterr().out


def test_annotation_cap_keeps_every_id_in_the_summary(tree: Path, monkeypatch, capsys) -> None:
    summary = tree / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    mod.main([str(tree / "junit.xml"), "--label", "s", "--root", str(tree), "--max-annotations", "1"])
    assert capsys.readouterr().out.count("::warning ") == 1
    text = summary.read_text()
    assert "UNRESOLVED tests.gone.test_removed::test_lost" in text
    assert "1 failing testcase(s) could not be mapped" in text
    assert "test_removed" not in text.split("Reproduce only these:")[1].split("\n")[0]


def test_green_report_says_zero(tree: Path, monkeypatch) -> None:
    (tree / "green.xml").write_text('<testsuite><testcase classname="tests.scripts.test_a" name="t"/></testsuite>')
    output = tree / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert mod.main([str(tree / "green.xml"), "--label", "s", "--root", str(tree)]) == 0
    assert output.read_text() == "failing_count=0\n"


@pytest.mark.parametrize("content", [None, "<testsuite><testcase"])
def test_unreadable_report_is_unknown_never_zero(tree: Path, monkeypatch, capsys, content) -> None:
    report = tree / "bad.xml"
    if content is not None:
        report.write_text(content)
    output = tree / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert mod.main([str(report), "--label", "s", "--root", str(tree)]) == 0
    assert output.read_text() == "failing_count=unknown\n"
    assert "::warning title=failing tests UNKNOWN" in capsys.readouterr().out


def test_workflow_command_data_is_escaped() -> None:
    assert mod._escape("a%b\nc\rd") == "a%25b%0Ac%0Dd"


def test_red_shard_with_no_failing_testcase_is_unknown(tree: Path, monkeypatch, capsys) -> None:
    (tree / "empty.xml").write_text("<testsuite/>")
    output, summary = tree / "output", tree / "summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert mod.main([str(tree / "empty.xml"), "--label", "s", "--root", str(tree), "--pytest-rc", "5"]) == 0
    assert output.read_text() == "failing_count=unknown\n"
    assert "pytest exited 5" in capsys.readouterr().out
    assert "### Failing tests (s): UNKNOWN" in summary.read_text()
