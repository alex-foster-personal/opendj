"""``scripts.ci_fast_cancel`` cancels a run only on a failure that is NEW against main.

Single-line intent:
  - if a failure absent from main's red set does not cancel the run then broken
  - if a failure that is also red on main cancels the run then broken
  - if a missing baseline cancels anything, or exits 0, then broken
  - if a log with no failure identity cancels the run then broken
  - if --dry-run calls gh then broken
  - if a GENUINE verdict under Actions leaves no annotation or summary then a cancelled job hides it
  - if an UNKNOWN verdict under Actions is not a warning then a missing baseline reads as evidence
  - if this module's identities differ from scripts.ci_failure_ids' for one log then the
    subtraction is between two formats and every known trunk red reads GENUINE

[if] the fast tier fails on a genuine identity [then] the run is cancelled, [else stop].

The baselines here are built by the SAME function that writes the real cache
(`scripts.ci_failure_ids.failed_identities`, via `scripts.ci_main_red`) rather than
hand-written. Until Thu 24 Sep 2026 they were hand-written WITHOUT the `FAILED `/`ERROR `
prefix the producer emits, which is the format this module's own parser used, so fixture
and code shared one wrong assumption and the suite passed while the subtraction could
never subtract anything. A fixture that shares the defect under test is not a control.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import ci_failure_ids, ci_fast_cancel

pytestmark = pytest.mark.requirement("INFRA-03")

LOG = """\
tests/a/test_x.py::test_one PASSED
=========================== short test summary info ============================
FAILED tests/a/test_x.py::test_two - assert 0 == 1
ERROR tests/b/test_y.py::test_three[param-1] - fixture broke
= 2 failed, 1 passed in 3.21s =
"""


def _baseline(tmp_path: Path, identities: list[str]) -> Path:
    path = tmp_path / "main-red.json"
    shape = {
        "identities": identities,
        "failed_job_names": [],
        "main_sha": "abc",
        "unreadable_job_names": [],
    }
    path.write_text(json.dumps(shape), encoding="utf-8")
    return path


def _log(tmp_path: Path, text: str = LOG) -> Path:
    path = tmp_path / "pytest.log"
    path.write_text(text, encoding="utf-8")
    return path


def _forbid_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(*_a: object, **_k: object) -> None:
        pytest.fail("gh was called")

    monkeypatch.setattr(ci_fast_cancel.subprocess, "run", _refuse)


def test_failed_identities_read_pytest_summary_lines() -> None:
    """if the ERROR line or a parametrized id is dropped then a genuine red reads as none"""
    assert ci_fast_cancel.failed_identities(LOG) == {
        "FAILED tests/a/test_x.py::test_two",
        "ERROR tests/b/test_y.py::test_three[param-1]",
    }


def test_genuine_failure_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """if a failure absent from main's red set does not cancel the run then broken"""
    calls: list[list[str]] = []
    monkeypatch.setattr(ci_fast_cancel.subprocess, "run", lambda cmd, **_: calls.append(cmd))
    log = _log(tmp_path)
    baseline = _baseline(tmp_path, ["FAILED tests/a/test_x.py::test_two"])
    rc = ci_fast_cancel.main(
        ["--log", str(log), "--run-id", "777", "--main-red-json", str(baseline)]
    )
    assert rc == 0
    assert calls == [["gh", "run", "cancel", "777"]]
    out = capsys.readouterr().out
    assert "genuine ERROR tests/b/test_y.py::test_three[param-1]" in out
    assert "cancelled run 777" in out


def test_all_known_on_main_does_not_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """if a failure that is also red on main cancels the run then broken"""
    _forbid_gh(monkeypatch)
    log = _log(tmp_path)
    baseline = _baseline(
        tmp_path,
        ["FAILED tests/a/test_x.py::test_two", "ERROR tests/b/test_y.py::test_three[param-1]"],
    )
    rc = ci_fast_cancel.main(
        ["--log", str(log), "--run-id", "777", "--main-red-json", str(baseline)]
    )
    assert rc == 0
    assert "cancelling nothing" in capsys.readouterr().out


def test_missing_baseline_is_unknown_and_cancels_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """if a missing baseline cancels anything, or exits 0, then broken"""
    _forbid_gh(monkeypatch)
    log = _log(tmp_path)
    absent = tmp_path / "absent.json"
    rc = ci_fast_cancel.main(
        ["--log", str(log), "--run-id", "777", "--main-red-json", str(absent)]
    )
    assert rc == ci_fast_cancel.EXIT_UNKNOWN
    assert "UNKNOWN" in capsys.readouterr().out


def test_no_identity_does_not_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """if a log with no failure identity (cap kill) cancels the run then broken"""
    _forbid_gh(monkeypatch)
    log = _log(tmp_path, "Terminated\n")
    baseline = _baseline(tmp_path, [])
    rc = ci_fast_cancel.main(
        ["--log", str(log), "--run-id", "777", "--main-red-json", str(baseline)]
    )
    assert rc == 0
    assert "no failing test identity" in capsys.readouterr().out


def test_dry_run_never_calls_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """if --dry-run calls gh then broken"""
    _forbid_gh(monkeypatch)
    log = _log(tmp_path)
    baseline = _baseline(tmp_path, [])
    rc = ci_fast_cancel.main(
        ["--log", str(log), "--run-id", "777", "--main-red-json", str(baseline), "--dry-run"]
    )
    assert rc == 0
    assert "dry run, gh not called" in capsys.readouterr().out


def test_genuine_under_actions_writes_an_error_annotation_and_step_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """if a GENUINE verdict leaves no annotation or summary then a cancelled job hides it"""
    _forbid_gh(monkeypatch)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    log = _log(tmp_path)
    baseline = _baseline(tmp_path, ["FAILED tests/a/test_x.py::test_two"])
    rc = ci_fast_cancel.main(
        ["--log", str(log), "--run-id", "777", "--main-red-json", str(baseline), "--dry-run",
         "--leg", "2 of 4"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "::error title=fast tier leg 2 of 4: GENUINE red (DRY RUN, nothing cancelled)::" in out
    assert "tests/b/test_y.py::test_three[param-1]" in out
    written = summary.read_text(encoding="utf-8")
    assert "### fast tier leg 2 of 4: GENUINE red" in written
    assert "- `ERROR tests/b/test_y.py::test_three[param-1]`" in written
    assert "test_two" not in written, "a known identity must not be reported as genuine"


def test_unknown_under_actions_is_a_warning_not_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """if an UNKNOWN verdict is not a warning then a missing baseline reads as evidence"""
    _forbid_gh(monkeypatch)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    log = _log(tmp_path)
    rc = ci_fast_cancel.main(
        ["--log", str(log), "--run-id", "777", "--main-red-json", str(tmp_path / "absent.json"),
         "--leg", "1 of 4"]
    )
    assert rc == ci_fast_cancel.EXIT_UNKNOWN
    out = capsys.readouterr().out
    assert "::warning title=fast tier leg 1 of 4: cancel decision UNKNOWN::" in out
    assert "NOT evidence" in summary.read_text(encoding="utf-8")


def test_outside_actions_no_annotation_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """if annotations print outside Actions then local runs spray ::error lines"""
    _forbid_gh(monkeypatch)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    log = _log(tmp_path)
    baseline = _baseline(tmp_path, [])
    ci_fast_cancel.main(
        ["--log", str(log), "--run-id", "777", "--main-red-json", str(baseline), "--dry-run"]
    )
    assert "::error" not in capsys.readouterr().out


# --------------------------------------------------------------------------------------
# Parity with the baseline's producer. `failed - baseline` is a set subtraction between a
# log this module parses and a cache `scripts.ci_main_red` parsed, so the two are only
# comparable while they speak ONE identity format.
# --------------------------------------------------------------------------------------

#: Shapes drawn from the ledger, not from a known-good corpus: a plain failure, an ERROR,
#: a parametrized id holding whitespace, one holding ` - ` inside its parameters, and the
#: adversarial id that quotes a whole failure line (tests/scripts/test_ci_failure_ids.py).
PARITY_LOG = """\
=========================== short test summary info ============================
FAILED tests/a/test_x.py::test_two - assert 0 == 1
ERROR tests/b/test_y.py::test_three[param-1] - fixture broke
FAILED tests/mik/test_mikdb_read.py::test_strip_energy_prefix[1979 - Remaster-1979 - Remaster]
FAILED tests/agentic/test_p.py::test_q[No errors appear anywhere.] - assert False
FAILED tests/scripts/test_z.py::test_w[FAILED tests/test_a.py::test_b - AssertionError]
PASSED tests/c/test_z.py::test_ok
= 5 failed, 1 passed in 3.21s =
"""


def test_the_identities_are_the_watchers_own_not_a_second_parser() -> None:
    """if this module's identities differ from the watcher's then the subtraction compares
    two formats and can never subtract anything"""
    assert ci_fast_cancel.failed_identities(PARITY_LOG) == ci_failure_ids.failed_identities(
        PARITY_LOG
    )
    # Negative control: the comparison above can report a difference. A parser that read
    # nothing would satisfy it just as well, so pin what it actually found.
    assert len(ci_fast_cancel.failed_identities(PARITY_LOG)) == 5
    assert ci_fast_cancel.failed_identities(PARITY_LOG) != ci_failure_ids.failed_identities(
        PARITY_LOG.replace("test_two", "test_renamed")
    )


def test_a_leg_failing_only_main_reds_is_not_cancelled_in_the_baselines_real_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """if a leg whose every failure is already red on main cancels the run then armed
    fail-fast kills runs for trunk's own breakage

    The regression this pins: the baseline is built by the producer's own parser, so a
    fixture cannot quietly restate this module's format. With the two parsers disagreeing
    the subtraction removed nothing and this read GENUINE.
    """
    _forbid_gh(monkeypatch)
    log = _log(tmp_path, PARITY_LOG)
    baseline = _baseline(tmp_path, sorted(ci_failure_ids.failed_identities(PARITY_LOG)))
    rc = ci_fast_cancel.main(
        ["--log", str(log), "--run-id", "777", "--main-red-json", str(baseline)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "every failure is already red on main" in out
    assert "GENUINE" not in out


def test_one_new_failure_beside_five_known_ones_is_still_genuine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """if a real regression hides behind main's own reds then fail-fast stops firing at all

    The opposite direction of the test above. Making every verdict KNOWN would satisfy
    that one perfectly and disarm the feature, so this is the control that refuses it.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(ci_fast_cancel.subprocess, "run", lambda cmd, **_: calls.append(cmd))
    log = _log(tmp_path, PARITY_LOG + "FAILED tests/new/test_n.py::test_new - boom\n")
    baseline = _baseline(tmp_path, sorted(ci_failure_ids.failed_identities(PARITY_LOG)))
    rc = ci_fast_cancel.main(
        ["--log", str(log), "--run-id", "777", "--main-red-json", str(baseline)]
    )
    assert rc == 0
    assert calls == [["gh", "run", "cancel", "777"]]
    out = capsys.readouterr().out
    assert "genuine FAILED tests/new/test_n.py::test_new" in out
    assert "test_strip_energy_prefix" not in out, "a known identity must not read as genuine"
