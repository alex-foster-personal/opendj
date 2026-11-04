"""``scripts.ci_fast_tier_never`` lists the modules whose over-ceiling tests cost a leg time.

Single-line intent:
  - if a module whose over-ceiling tests cost >= --min-seconds in most legs is not listed then broken
  - if a module reaching it in half of its legs or fewer is listed then broken
  - if many under-ceiling tests summing past --min-seconds list a module then broken
  - if a JUnit report with no `file` attribute yields an empty list instead of an error then broken

[if] leg JUnit is given [then] exactly the modules over the bar in most legs are listed, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import ci_fast_tier_never as never

pytestmark = pytest.mark.requirement("INFRA-03")


def _junit(tmp_path: Path, name: str, cases: list[tuple[str, float]]) -> Path:
    rows = "".join(
        f'<testcase classname="c" name="t{i}" file="{module}" time="{seconds}"/>'
        for i, (module, seconds) in enumerate(cases)
    )
    path = tmp_path / name
    path.write_text(f'<testsuites><testsuite name="pytest">{rows}</testsuite></testsuites>')
    return path


def _list(tmp_path: Path, *legs: list[tuple[str, float]]) -> list[str]:
    paths = [str(_junit(tmp_path, f"leg{i}.xml", cases)) for i, cases in enumerate(legs)]
    out = tmp_path / "never.txt"
    assert never.main([*paths, "--ceiling", "0.5", "--min-seconds", "2", "--out", str(out)]) == 0
    return [line for line in out.read_text().splitlines() if line and not line.startswith("#")]


def test_a_module_over_the_bar_in_most_legs_is_listed_and_one_in_half_is_not(
    tmp_path: Path,
) -> None:
    """[if] over the bar in 2 of 3 legs [then] listed; in 1 of 2 [then] not, [else stop]."""
    shared = [("tests/a.py", 1.5), ("tests/a.py", 1.0)]
    once = [("tests/b.py", 3.0)]
    listed = _list(
        tmp_path,
        shared + once,
        [*shared, ("tests/b.py", 0.1)],
        [("tests/a.py", 0.1)],
    )
    assert listed == ["tests/a.py"]


def test_many_under_ceiling_tests_never_list_a_module(tmp_path: Path) -> None:
    """[if] a module's tests are each under the ceiling [then] their sum does not list it,
    [else stop]."""
    many_quick = [("tests/webui/test_many.py", 0.4)] * 20  # 8 s, every test under 0.5 s
    assert _list(tmp_path, many_quick, many_quick) == []


def test_a_report_without_file_attributes_is_an_error(tmp_path: Path) -> None:
    """[if] no testcase names its file [then] JUnitError naming the report, [else stop]."""
    path = tmp_path / "bad.xml"
    path.write_text('<testsuites><testsuite><testcase name="t" time="9"/></testsuite></testsuites>')
    with pytest.raises(never.JUnitError, match=r"bad\.xml"):
        never.main([str(path), "--ceiling", "0.5", "--min-seconds", "2", "--out", str(tmp_path / "o")])
