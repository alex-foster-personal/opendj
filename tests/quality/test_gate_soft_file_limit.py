"""Report-only soft file-length counts beside the strict size ratchet.

file_size.soft_over_* names files past the 1000-line line. The gate prints
that list and never fails on it. file_size.over_limit_* still counts files
past 1500 lines and can fail once a run is past its allowance plus slack.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import quality_gate as qg

_SOFT_KEYS = ("file_size.soft_over_python", "file_size.soft_over_frontend")
_LIST_MAX = qg.CFG.SOFT_LIST_MAX


def _write_lines(directory: Path, name: str, count: int) -> Path:
    path = directory / name
    path.write_text("x = 1\n" * count, encoding="utf-8", newline="\n")
    return path


def _metric(metrics: list[qg.Metric], key: str) -> qg.Metric:
    matches = [item for item in metrics if item.key == key]
    found = len(matches) == 1
    assert found, f"if size metrics are built then {key} appears once, but it did not"
    return matches[0]


def test_soft_metric_reports_without_failing(tmp_path: Path) -> None:
    """If a file is past the soft line only then the gate reports it and does not fail."""
    python_files = [
        _write_lines(tmp_path, "twelve_hundred.py", 1200),
        _write_lines(tmp_path, "eight_hundred.py", 800),
    ]
    frontend_files = [_write_lines(tmp_path, "tiny.ts", 10)]
    metrics = qg._file_size_metrics(python_files, frontend_files, repo=tmp_path, list_max=_LIST_MAX)
    soft = _metric(metrics, "file_size.soft_over_python")
    assert soft.value == 1, f"if one python file is past 1000 lines then soft_over is 1, but it was {soft.value:g}"
    assert "twelve_hundred.py" in soft.detail, (
        f"if the 1200-line file is over the soft line then detail names it, but {soft.detail!r}"
    )
    assert "1200 lines" in soft.detail, (
        f"if that file is 1200 lines long then detail says 1200 lines, but {soft.detail!r}"
    )
    assert "eight_hundred.py" not in soft.detail, (
        f"if an 800-line file is under the soft line then detail omits it, but {soft.detail!r}"
    )
    huge = qg.Metric(soft.key, 9999, soft.unit, soft.detail)
    regressions, ratchets, unknown, _ = qg._compare([huge], {}, {})
    quiet = not (regressions or ratchets or unknown)
    assert quiet, (
        "if soft_over is 9999 with no baseline then regressions, ratchets and "
        f"unknown stay empty, but they were {regressions!r} {ratchets!r} {unknown!r}"
    )
    tiny_allowance = {"file_size.soft_over_python": 1.0}
    regressions, ratchets, unknown, _ = qg._compare([huge], tiny_allowance, {})
    quiet = not (regressions or ratchets or unknown)
    assert quiet, (
        "if soft_over is 9999 against a tiny allowance then regressions, "
        f"ratchets and unknown stay empty, but they were {regressions!r} {ratchets!r} {unknown!r}"
    )


def test_strict_limit_still_fails_past_slack(tmp_path: Path) -> None:
    """If a file is past 1500 lines then the strict count still fails beyond slack."""
    python_files = [
        _write_lines(tmp_path, "at_limit.py", 1500),
        _write_lines(tmp_path, "past_limit.py", 1501),
    ]
    frontend_files = [_write_lines(tmp_path, "tiny.ts", 10)]
    metrics = qg._file_size_metrics(python_files, frontend_files, repo=tmp_path, list_max=_LIST_MAX)
    over = _metric(metrics, "file_size.over_limit_python")
    assert over.value == 1, (
        "if one file is 1501 lines and one is 1500 then over_limit_python "
        f"counts only the 1501-line file, but the count was {over.value:g}"
    )
    # N is an allowance. Slack is one file, keyed by metric name: dict[str, float].
    allowance_n = 4.0
    baseline = {"file_size.over_limit_python": allowance_n}
    slack: dict[str, float] = {"file_size.over_limit_python": 1.0}
    measured_n_plus_1 = qg.Metric(over.key, allowance_n + 1, over.unit)
    measured_n_plus_2 = qg.Metric(over.key, allowance_n + 2, over.unit)
    regressions, _, _, within_slack = qg._compare([measured_n_plus_1], baseline, slack)
    assert not regressions, (
        f"if over_limit_python measures N+1 with slack 1 then regressions stay empty, but they were {regressions!r}"
    )
    assert within_slack, (
        "if over_limit_python measures N+1 with slack 1 then the compare records "
        "it as within slack, but the within-slack list was empty"
    )
    regressions, _, _, _ = qg._compare([measured_n_plus_2], baseline, slack)
    assert regressions, (
        "if over_limit_python measures N+2 with slack 1 then the gate reports a regression, but regressions were empty"
    )


def test_twelve_hundred_lines_counts_as_soft_only(tmp_path: Path) -> None:
    """If a file is 1200 lines then only the soft count includes it."""
    python_files = [_write_lines(tmp_path, "twelve_hundred.py", 1200)]
    frontend_files = [_write_lines(tmp_path, "tiny.ts", 10)]
    metrics = qg._file_size_metrics(python_files, frontend_files, repo=tmp_path, list_max=_LIST_MAX)
    over = _metric(metrics, "file_size.over_limit_python")
    soft = _metric(metrics, "file_size.soft_over_python")
    assert over.value == 0, f"if a file is 1200 lines then over_limit_python skips it, but the count was {over.value:g}"
    assert soft.value == 1, f"if a file is 1200 lines then soft_over_python counts it, but the count was {soft.value:g}"


def test_soft_keys_stay_out_of_the_gate_and_the_baseline() -> None:
    """If a count is report-only then it is outside the hard gates and the baseline."""
    recorded = json.loads(qg.BASELINE.read_text(encoding="utf-8"))["metrics"]
    for key in _SOFT_KEYS:
        assert key in qg.REPORT_ONLY, (
            f"if {key} is a soft file-length count then REPORT_ONLY contains it, but it did not"
        )
        assert key not in qg.HARD_ZERO, (
            f"if {key} is a soft file-length count then HARD_ZERO omits it, but it was present"
        )
        assert key not in recorded, (
            f"if {key} is a soft file-length count then baseline metrics omit it, but it was present"
        )


def test_soft_detail_keeps_the_true_count_past_the_name_cap(tmp_path: Path) -> None:
    """If more files pass the soft line than the name cap then the count stays whole."""
    names = ("aa.py", "bb.py", "cc.py", "dd.py", "ee.py")
    files = [_write_lines(tmp_path, name, 1100) for name in names]
    metrics = qg._language_size_metrics(
        files,
        language="python",
        strict_limit=qg.CFG.PY_FILE_LIMIT,
        soft_limit=qg.CFG.PY_FILE_SOFT_LIMIT,
        repo=tmp_path,
        list_max=2,
    )
    soft = _metric(metrics, "file_size.soft_over_python")
    assert soft.value == 5, f"if five files pass 1000 lines then the count is 5, but it was {soft.value:g}"
    parts = soft.detail.split("; ")
    assert parts[-1] == "+3 more", (
        f"if three names are hidden then detail ends with '+3 more', but it was {soft.detail!r}"
    )
    named = parts[:-1]
    assert len(named) == 2, f"if the name cap is 2 then detail names exactly two files, but it named {named!r}"
    shown = {part.split(" ", 1)[0] for part in named}
    assert shown <= set(names), (
        f"if the cap keeps two names then both are files that were sized, but it named {shown!r}"
    )
    assert all(part.endswith("(1100 lines)") for part in named), (
        f"if the capped names are the 1100-line files then detail says so, but it was {named!r}"
    )


def test_empty_file_list_raises(tmp_path: Path) -> None:
    """If the file list is empty then sizing raises instead of reporting zero."""
    raised = False
    try:
        qg._file_size_metrics([], [], repo=tmp_path, list_max=_LIST_MAX)
    except RuntimeError as exc:
        raised = True
        assert "no files found to size" in str(exc), (
            f"if the file list is empty then the error says no files were found, but it said {exc!r}"
        )
    assert raised, "if the file list is empty then sizing raises RuntimeError, but it returned a clean result"
