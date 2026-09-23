"""Issue #1155: a regression that main already carries is not this change's fault.

A merge can land an over-allowance metric that neither parent exceeded (each
side adds lines to the same file, each stays under the limit, the union
crosses it). Once that sits on main, every later PR measures the same
over-allowance metric and today's gate fails each author for a regression they
inherited. This suite pins the two ways the gate must tell the cases apart:

  - base main measured AT OR ABOVE this run's value  -> INHERITED, run passes,
    and the message names main as the owner (a trunk regression, not yours).
  - base main measured BELOW this run's value        -> the change made an
    already-bad number worse, so it stays a hard REGRESSION and the run fails.
  - the merge base cannot be measured                -> base_compare UNKNOWN
    (exit 2), never REGRESSION; the run says why out loud. Never a silent pass.

Regression lines:
  - if main is already over at this run's value but the run reports REGRESSION
    and exits 1 then the gate has stopped distinguishing an inherited failure,
    so broken
  - if this run adds to an over-allowance metric (base below the run) but the
    gate passes or prints INHERITED then it over-shot in the other direction
    and let a real regression through, so broken
  - if the merge-base main cannot be measured but the gate prints REGRESSION or
    passes silently instead of UNKNOWN with a why, so broken
"""

from __future__ import annotations

import pytest

from scripts import quality_gate as qg

_METRIC_KEY = "file_size.over_limit_python"
_ALLOWANCE = 49.0
_BASE_SHORT = "6407131df"


def _m(value: float) -> qg.Metric:
    return qg.Metric(_METRIC_KEY, value, "files > 600 lines")


def _run_gate(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    metric_value: float,
    base_values: dict[str, float] | None = None,
    resolve: tuple[str | None, str] = (_BASE_SHORT, ""),
    measure_error: str = "",
    fail_if_called: bool = False,
) -> tuple[int, str]:
    """Run the real gate over one canned over-allowance metric.

    Every heavy or environment-dependent seam is replaced with a fake, so the
    test drives the classification logic in main() without ruff, node, or a
    second git tree. `base_values=None` makes the base measurement fail with
    `measure_error`. `fail_if_called` proves a green run never pays for the
    merge-base re-measure.
    """

    def _fake_eval() -> list[qg.Metric]:
        return [_m(metric_value)]

    def _measure(_sha: str, _owners: list[str]) -> tuple[dict[str, float] | None, str]:
        if base_values is None:
            return None, measure_error or "boom"
        return base_values, ""

    def _fail(*_: object) -> None:
        raise AssertionError("the merge-base re-measure ran on a run that did not regress")

    monkeypatch.setattr(qg, "EVALUATORS", (qg.Evaluator("fake", "fake title", _fake_eval),))
    monkeypatch.setattr(qg, "_load_baseline", lambda: {_METRIC_KEY: _ALLOWANCE})
    # #1219 gave metrics a slack band; this suite is about inheritance, so it
    # pins slack to nothing and keeps measuring the bare-allowance behavior.
    monkeypatch.setattr(qg, "_load_slack", dict)
    monkeypatch.setattr(qg, "_hotspots", lambda: qg.HotspotResult([]))
    monkeypatch.setattr(qg, "_resolve_base", _fail if fail_if_called else lambda: resolve)
    monkeypatch.setattr(
        qg, "_measure_owners_at_base",
        _fail if fail_if_called else _measure,
    )

    code = qg.main(["--only", "fake"])
    return code, capsys.readouterr().out


# ----- the inheritance the issue is about ----------------------------------


def test_base_main_already_over_at_same_value_is_INHERITED_and_passes(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If main is already at 50 and this run is 50 then it is not this change."""
    code, out = _run_gate(
        capsys, monkeypatch, metric_value=50.0, base_values={_METRIC_KEY: 50.0},
        resolve=(_BASE_SHORT, ""),
    )
    assert code == 0, (
        "if the merge-base main is already at this run's value then the gate "
        f"must pass, but it exited {code}\n{out}"
    )
    assert "INHERITED" in out, (
        "if main is already over at the same value then the line must print "
        f"INHERITED where REGRESSION would have been\n{out}"
    )
    assert f"main ({_BASE_SHORT}) is ALSO at 50" in out, (
        "the message must name main and the value it measured so the author "
        f"reads it as trunk's problem, not their own\n{out}"
    )
    assert "trunk regression, not yours" in out
    assert "FAIL:" not in out, f"an inherited metric must not fail the run\n{out}"


def test_making_an_already_over_metric_worse_stays_a_hard_regression(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If main is at 50 and this run is 51 then 51 is this change's fault."""
    code, out = _run_gate(
        capsys, monkeypatch, metric_value=51.0, base_values={_METRIC_KEY: 50.0},
        resolve=(_BASE_SHORT, ""),
    )
    assert code == 1, (
        "if this run is above main's already-over value then the gate must "
        f"fail, but it exited {code}\n{out}"
    )
    assert "FAIL: 1 metric(s) got worse." in out
    assert "INHERITED" not in out, (
        "a run that made an already-over metric worse must stay a REGRESSION, "
        f"not be relabeled INHERITED\n{out}"
    )
    assert "this change is worse than main" in out, (
        "the run must say why the metric stayed a regression: it is worse than "
        f"what main measures\n{out}"
    )


def test_a_clean_base_stays_a_regression(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If main is under the allowance and this run crossed it, the run is red."""
    code, out = _run_gate(
        capsys, monkeypatch, metric_value=50.0, base_values={_METRIC_KEY: 49.0},
        resolve=(_BASE_SHORT, ""),
    )
    assert code == 1, (
        "if main is not over the allowance then this run introduced the "
        f"regression and must fail, but it exited {code}\n{out}"
    )
    assert "FAIL: 1 metric(s) got worse." in out
    assert "INHERITED" not in out


def test_a_green_run_never_pays_for_the_merge_base_re_measure(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If nothing regressed, the base scan must not run at all (Q-11 cost)."""
    code, out = _run_gate(
        capsys, monkeypatch, metric_value=_ALLOWANCE, fail_if_called=True,
    )
    assert code == 0
    assert "PASS: nothing got worse." in out


# ----- undecidable base: never downgrade, never guess ----------------------


def test_unmeasurable_merge_base_reports_unknown_not_regression(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] merge-base cannot be measured [then] UNKNOWN exit 2, [else stop]."""
    code, out = _run_gate(
        capsys, monkeypatch, metric_value=50.0, measure_error="the base tree has no apps/ to scan",
    )
    assert code == 2, (
        "if the merge-base main cannot be measured then the gate must exit 2 "
        f"(UNKNOWN), but it exited {code}\n{out}"
    )
    assert "UNKNOWN: base_compare:" in out, (
        "the run must report base_compare as UNKNOWN rather than a verdict\n"
        f"{out}"
    )
    assert "REGRESSION" not in out, (
        "inheritance-eligible metrics must not print REGRESSION when the base "
        f"compare is undecidable\n{out}"
    )
    assert "FAIL:" not in out
    assert "INHERITED" not in out
    assert "cannot re-measure fake on merge-base main" in out, (
        "the run must say why it could not classify the metric rather than "
        f"passing silently\n{out}"
    )


def test_resolve_base_git_failure_is_unknown_not_regression(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] merge-base git fails [then] UNKNOWN exit 2, [else stop]."""
    code, out = _run_gate(
        capsys,
        monkeypatch,
        metric_value=50.0,
        resolve=(None, "git merge-base HEAD origin/main failed (exit 1)"),
    )
    assert code == 2, f"expected UNKNOWN exit 2, got {code}\n{out}"
    assert "UNKNOWN: base_compare:" in out
    assert "REGRESSION" not in out
    assert "FAIL:" not in out


def test_hard_zero_regression_still_fails_when_base_compare_unknown(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] HARD_ZERO regresses and base is unknown [then] exit 1, [else stop]."""
    hard_zero = next(iter(qg.HARD_ZERO))

    def _fake_eval() -> list[qg.Metric]:
        return [
            qg.Metric(hard_zero, 1.0, "broken"),
            _m(50.0),
        ]

    def _measure(_sha: str, _owners: list[str]) -> tuple[dict[str, float] | None, str]:
        return None, "worktree add failed"

    monkeypatch.setattr(qg, "EVALUATORS", (qg.Evaluator("fake", "fake title", _fake_eval),))
    monkeypatch.setattr(
        qg, "_load_baseline", lambda: {hard_zero: 0.0, _METRIC_KEY: _ALLOWANCE}
    )
    monkeypatch.setattr(qg, "_load_slack", dict)
    monkeypatch.setattr(qg, "_hotspots", lambda: qg.HotspotResult([]))
    monkeypatch.setattr(qg, "_resolve_base", lambda: (_BASE_SHORT, ""))
    monkeypatch.setattr(qg, "_measure_owners_at_base", _measure)

    code = qg.main(["--only", "fake"])
    out = capsys.readouterr().out
    assert code == 1, (
        "a measured HARD_ZERO regression must still fail even when base_compare "
        f"is UNKNOWN, but it exited {code}\n{out}"
    )
    assert "REGRESSION" in out
    assert "FAIL: 1 metric(s) got worse." in out
    assert "UNKNOWN: base_compare:" in out


def test_worktree_add_failure_includes_stderr_in_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] worktree add fails [then] stderr is in the reason, [else stop]."""
    monkeypatch.setattr(
        qg,
        "_run_capture",
        lambda *args, **kwargs: (1, "", "fatal: worktree path already exists"),
    )
    values, reason = qg._measure_owners_at_base("abc123def456", ["size"])
    assert values is None
    assert "fatal: worktree path already exists" in reason
    assert "exit 1" in reason


def test_head_is_on_main_so_there_is_nothing_to_inherit_from(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A push-to-main run is the detector: it must never relabel its own red."""
    code, out = _run_gate(
        capsys, monkeypatch, metric_value=50.0, base_values={_METRIC_KEY: 50.0},
        resolve=(None, "HEAD is itself on main; there is no other main to inherit from"),
    )
    assert code == 1, (
        "if HEAD is the trunk tip then a regression there is the merge's fault "
        f"and must stay red, but it exited {code}\n{out}"
    )
    assert "INHERITED" not in out, (
        "on a push-to-main run the metric must keep its unqualified REGRESSION "
        f"or the detector goes silent the moment trunk breaks\n{out}"
    )


def test_a_metric_missing_from_the_base_measurement_stays_a_regression(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the base run did not report the metric, do not guess it was inherited."""
    code, out = _run_gate(
        capsys, monkeypatch, metric_value=50.0, base_values={},
        resolve=(_BASE_SHORT, ""),
    )
    assert code == 1
    assert "REGRESSION" in out and "INHERITED" not in out
    assert "the merge-base main run (6407131df) did not report it" in out


# ----- only plain ratchet metrics can be inherited -------------------------


def test_hard_zero_and_report_only_metrics_are_never_eligible() -> None:
    """A broken contract or a report-only number cannot be 'over on main too'."""
    hard_zero = next(iter(qg.HARD_ZERO))
    report_only = next(iter(qg.REPORT_ONLY))
    baseline = {hard_zero: 99.0, report_only: 1.0}
    metrics = [
        qg.Metric(hard_zero, 1.0, "x"),
        qg.Metric(report_only, 99.0, "x"),
    ]
    assert not qg._ratchet_exceeded(metrics[0], baseline), (
        "if a hard-zero metric were eligible then an architecture rule with a "
        "grown allowance could excuse itself by pointing at a broken main"
    )
    assert not qg._ratchet_exceeded(metrics[1], baseline), (
        "if a report-only metric were eligible then a number that never fails "
        "the gate could be relabeled as inherited, which is meaningless"
    )


def test_an_equal_metric_is_not_a_regression_at_all() -> None:
    """A metric exactly at its allowance never reaches the inherited logic."""
    assert not qg._ratchet_exceeded(_m(_ALLOWANCE), {_METRIC_KEY: _ALLOWANCE})


# ----- message shape -------------------------------------------------------


def test_the_INHERITED_block_names_the_metric_value_and_main() -> None:
    m = _m(50.0)
    line1, line2 = qg._inherited_block(m, 50.0, _BASE_SHORT, {_METRIC_KEY: _ALLOWANCE})
    assert line1 == f"INHERITED  {_METRIC_KEY}: 50 > 49 allowed"
    assert line2 == (
        f"          main ({_BASE_SHORT}) is ALSO at 50 - this is a trunk "
        "regression, not yours"
    )
