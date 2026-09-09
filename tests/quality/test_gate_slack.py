"""Issue #1219: the ratchet needs one PR's worth of headroom, not zero.

`ops/quality/baseline.json` is hand-lowered to whatever each landing PR
achieved, so burning debt down never created slack -- the wall moved with you.
On Fri 5 Sep 2026 all seven count metrics sat at zero headroom simultaneously
(ruff.complexity 142/142, complexity.blocks_over_limit 144/144,
frontend.max_fan_out 35/35, frontend.unknown_casts 16/16,
file_size.over_limit_python 48/48, file_size.max_frontend 4042/4042,
file_size.over_limit_frontend 10/10) and three mergeable PRs were permanently
red on the gate and nothing else.

The fix is a declared per-metric `slack` band: a run is judged against
allowance + slack, allowances still only shrink, and the raw numbers still
print. This suite pins the four properties that make the band a budget rather
than a hole in the gate.

Regression lines:
  - if a metric exactly at allowance + slack fails the run then the band does
    not exist and the board is jammed again, so broken
  - if a metric at allowance + slack + 1 passes then the ceiling is not a
    ceiling and debt can grow unbounded, so broken
  - if a metric with no `slack` entry is judged against anything but its bare
    allowance then a missing key silently widened a gate, so broken
  - if --update-baseline drops the `slack` block then the next ratchet-down
    re-tightens every metric to zero headroom without saying so, so broken
  - if --update-baseline writes back a measurement that sits inside a slack
    band then each passing run banks the drift and the ceiling walks upward a
    band at a time, which is the shrink-only invariant broken by the very
    feature that was supposed to preserve it, so broken
  - if the #1159 INHERITED classification stops working (main at or above the
    run passes and names main; main below the run stays red) then this change
    broke the gate it was built on, so broken
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import quality_gate as qg

_KEY = "file_size.over_limit_python"
_ALLOWANCE = 48.0
_SLACK = 1.0
_BASE_SHORT = "84abf19dd"


def _m(value: float, key: str = _KEY) -> qg.Metric:
    return qg.Metric(key, value, "files > 600 lines")


def _run_gate(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    metric_value: float,
    slack: dict[str, float] | None = None,
    base_values: dict[str, float] | None = None,
) -> tuple[int, str]:
    """Run the real gate over one canned metric with a canned slack table.

    Every heavy seam (ruff, node, the merge-base worktree) is faked, so the
    test drives the slack arithmetic in main() and nothing else.
    """

    def _fake_eval() -> list[qg.Metric]:
        return [_m(metric_value)]

    def _measure(_sha: str, _owners: list[str]) -> tuple[dict[str, float] | None, str]:
        if base_values is None:
            return None, "no base measurement was canned for this test"
        return base_values, ""

    monkeypatch.setattr(qg, "EVALUATORS", (qg.Evaluator("fake", "fake title", _fake_eval),))
    monkeypatch.setattr(qg, "_load_baseline", lambda: {_KEY: _ALLOWANCE})
    monkeypatch.setattr(qg, "_load_slack", lambda: dict(slack or {}))
    monkeypatch.setattr(qg, "_hotspots", list)
    monkeypatch.setattr(qg, "_resolve_base", lambda: (_BASE_SHORT, ""))
    monkeypatch.setattr(qg, "_measure_owners_at_base", _measure)

    code = qg.main(["--only", "fake"])
    return code, capsys.readouterr().out


# ----- the band itself -----------------------------------------------------


def test_exactly_at_allowance_plus_slack_passes(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a PR lands one file over a 48 allowance with 1 slack then it passes."""
    code, out = _run_gate(
        capsys, monkeypatch, metric_value=_ALLOWANCE + _SLACK, slack={_KEY: _SLACK}
    )
    assert code == 0, (
        "if a metric sits exactly at allowance + slack then the gate must pass, "
        f"but it exited {code}\n{out}"
    )
    assert "WITHIN SLACK" in out, (
        "a run that used its slack must say so out loud; a silent pass is how "
        f"debt grows unwatched\n{out}"
    )
    assert "REGRESSION" not in out and "FAIL:" not in out


def test_one_past_allowance_plus_slack_fails(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a PR lands two files over a 48 allowance with 1 slack then it is red."""
    code, out = _run_gate(
        capsys,
        monkeypatch,
        metric_value=_ALLOWANCE + _SLACK + 1,
        slack={_KEY: _SLACK},
        base_values={_KEY: _ALLOWANCE},
    )
    assert code == 1, (
        "if a metric passes allowance + slack then the ceiling must fail the "
        f"run, but it exited {code}\n{out}"
    )
    assert "FAIL: 1 metric(s) got worse." in out
    assert "WITHIN SLACK" not in out


def test_within_slack_prints_both_raw_numbers(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report must keep printing the measurement, the allowance and the band."""
    code, out = _run_gate(capsys, monkeypatch, metric_value=49.0, slack={_KEY: _SLACK})
    assert code == 0
    assert f"{_KEY}: 49 > 48 allowed, inside the 1 slack (ceiling 49)" in out, (
        "the WITHIN SLACK line must name the measured value, the recorded "
        f"allowance and the ceiling, so nothing is hidden behind a pass\n{out}"
    )
    assert "used declared slack" in out, (
        f"the verdict must attribute the pass to the slack band\n{out}"
    )


def test_a_metric_without_slack_is_gated_at_its_bare_allowance(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If no slack is declared for a metric then it gets zero, not a default."""
    code, out = _run_gate(
        capsys,
        monkeypatch,
        metric_value=_ALLOWANCE + 1,
        slack={"some.other.metric": 99.0},
        base_values={_KEY: _ALLOWANCE},
    )
    assert code == 1, (
        "if a metric has no slack entry then one unit over its allowance must "
        f"still fail, but it exited {code}\n{out}"
    )
    assert "REGRESSION" in out and "WITHIN SLACK" not in out


def test_missing_slack_key_means_zero_in_the_comparison() -> None:
    """_compare with no slack table at all behaves exactly as it did pre-#1219."""
    regressions, ratchets, unknown, within = qg._compare([_m(49.0)], {_KEY: _ALLOWANCE})
    assert regressions and not within, (
        "if no slack table is passed then every metric is judged at its bare "
        f"allowance, but the gate reported within-slack {within}"
    )
    assert regressions[0] == f"{_KEY}: 49 > 48 allowed", (
        "the zero-slack regression line must stay byte-identical to the "
        f"pre-slack message, but it read {regressions[0]}"
    )
    assert not ratchets and not unknown


def test_slack_line_is_not_offered_as_a_ratchet() -> None:
    """A value above the allowance is never a ratchet, band or no band."""
    _, ratchets, _, within = qg._compare([_m(49.0)], {_KEY: _ALLOWANCE}, {_KEY: _SLACK})
    assert within and not ratchets, (
        "if slack use were reported as a ratchet then --update-baseline would "
        f"look like it was lowering a number it is raising: {within} / {ratchets}"
    )


def test_equal_to_allowance_stays_silent_with_slack_declared() -> None:
    """Trunk sitting exactly at its allowance is held, never 'within slack'."""
    regressions, ratchets, unknown, within = qg._compare(
        [_m(_ALLOWANCE)], {_KEY: _ALLOWANCE}, {_KEY: _SLACK}
    )
    assert not (regressions or ratchets or unknown or within), (
        "trunk at its own allowance must report nothing at all, but the gate "
        f"said {regressions or ratchets or unknown or within}"
    )


def test_trunk_at_its_allowance_is_never_red(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state that jammed the board: measured == allowance must pass clean."""
    code, out = _run_gate(capsys, monkeypatch, metric_value=_ALLOWANCE, slack={_KEY: _SLACK})
    assert code == 0 and "PASS: nothing got worse." in out, (
        "a tree exactly at its allowance must pass with nothing reported, but "
        f"it exited {code}\n{out}"
    )


# ----- #1159 inheritance still works on top of the band --------------------


def test_inherited_still_passes_when_main_is_at_the_same_value(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the ceiling, but main is there too: still INHERITED, still green."""
    code, out = _run_gate(
        capsys,
        monkeypatch,
        metric_value=51.0,
        slack={_KEY: _SLACK},
        base_values={_KEY: 51.0},
    )
    assert code == 0, (
        "if the merge-base main is already at this run's value then the gate "
        f"must still pass after #1219, but it exited {code}\n{out}"
    )
    assert "INHERITED" in out and "trunk regression, not yours" in out
    assert f"main ({_BASE_SHORT}) is ALSO at 51" in out


def test_a_regression_above_a_trunk_already_over_allowance_still_fails(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main is over the allowance at 50; this run adds to it and stays red."""
    code, out = _run_gate(
        capsys,
        monkeypatch,
        metric_value=51.0,
        slack={_KEY: _SLACK},
        base_values={_KEY: 50.0},
    )
    assert code == 1, (
        "if this run is above an already-over main then slack must not launder "
        f"it into a pass, but it exited {code}\n{out}"
    )
    assert "FAIL: 1 metric(s) got worse." in out
    assert "INHERITED" not in out
    assert "this change is worse than main" in out


def test_the_inherited_block_names_the_slack_when_one_is_declared() -> None:
    """The INHERITED line prints allowance and slack separately, never pre-added."""
    line1, _ = qg._inherited_block(_m(51.0), 51.0, _BASE_SHORT, {_KEY: _ALLOWANCE}, {_KEY: _SLACK})
    assert line1 == f"INHERITED  {_KEY}: 51 > 48 allowed + 1 slack", (
        f"the inherited line must show both numbers, but it read {line1}"
    )


def test_the_inherited_block_is_unchanged_without_slack() -> None:
    """With no slack declared the #1159 message must be byte-identical."""
    line1, _ = qg._inherited_block(_m(50.0), 50.0, _BASE_SHORT, {_KEY: 49.0})
    assert line1 == f"INHERITED  {_KEY}: 50 > 49 allowed"


# ----- the band survives a ratchet-down ------------------------------------


def test_update_baseline_carries_the_slack_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the baseline is rewritten then `slack` survives, like `burn_down`."""
    target = tmp_path / "baseline.json"
    target.write_text(
        json.dumps(
            {
                "generated": "t",
                "note": "n",
                "slack": {_KEY: _SLACK},
                "burn_down": {"SOME_TRAIN": {"what": "prose"}},
                "metrics": {_KEY: _ALLOWANCE},
            }
        )
    )
    monkeypatch.setattr(qg, "BASELINE", target)
    qg._write_baseline([_m(47.0)])
    rewritten = json.loads(target.read_text())
    assert rewritten["slack"] == {_KEY: _SLACK}, (
        "if --update-baseline drops the slack block then the next ratchet-down "
        f"silently re-tightens every gate to zero headroom: {rewritten.get('slack')}"
    )
    assert rewritten["metrics"][_KEY] == 47.0, "the allowance must still ratchet down"
    assert "burn_down" in rewritten, "the burn-down narrative must still survive"


def test_update_baseline_never_raises_an_allowance_a_slack_run_floated_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A within-slack measurement must NOT become the new allowance.

    Without this the band compounds: the run at 49 passes on a 48 allowance,
    --update-baseline banks 49, the next run passes at 50, and the ceiling
    walks up one band per update while the file still claims allowances only
    ever shrink.
    """
    target = tmp_path / "baseline.json"
    target.write_text(
        json.dumps(
            {
                "generated": "t",
                "note": "n",
                "slack": {_KEY: _SLACK},
                "metrics": {_KEY: _ALLOWANCE},
            }
        )
    )
    monkeypatch.setattr(qg, "BASELINE", target)
    qg._write_baseline([_m(_ALLOWANCE + _SLACK)])
    recorded = json.loads(target.read_text())["metrics"][_KEY]
    assert recorded == _ALLOWANCE, (
        "if a within-slack measurement is written back as the allowance then "
        f"the ceiling compounds one band per update; the file recorded {recorded}"
    )
    out = capsys.readouterr().out
    assert "ALLOWANCE KEPT" in out and "allowance kept at 48" in out, (
        f"a rewrite that declined to raise an allowance must say so\n{out}"
    )


def test_repeated_updates_cannot_walk_the_ceiling_upward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three passing within-slack runs in a row leave the allowance where it was."""
    target = tmp_path / "baseline.json"
    target.write_text(
        json.dumps(
            {
                "generated": "t",
                "note": "n",
                "slack": {_KEY: _SLACK},
                "metrics": {_KEY: _ALLOWANCE},
            }
        )
    )
    monkeypatch.setattr(qg, "BASELINE", target)
    for _ in range(3):
        allowance = json.loads(target.read_text())["metrics"][_KEY]
        qg._write_baseline([_m(allowance + _SLACK)])
    recorded = json.loads(target.read_text())["metrics"][_KEY]
    assert recorded == _ALLOWANCE, (
        "after three update-baseline runs at allowance + slack the allowance "
        f"must still be {_ALLOWANCE:g}, but it had walked to {recorded:g}"
    )


def test_update_baseline_still_lowers_an_improved_metric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ratchet still ratchets: a better number is recorded as before."""
    target = tmp_path / "baseline.json"
    target.write_text(
        json.dumps(
            {
                "generated": "t",
                "note": "n",
                "metrics": {_KEY: _ALLOWANCE},
            }
        )
    )
    monkeypatch.setattr(qg, "BASELINE", target)
    qg._write_baseline([_m(_ALLOWANCE - 3)])
    recorded = json.loads(target.read_text())["metrics"][_KEY]
    assert recorded == _ALLOWANCE - 3, (
        f"a burn-down must lower the allowance, but the file recorded {recorded}"
    )
    assert "ALLOWANCE KEPT" not in capsys.readouterr().out


def test_update_baseline_holds_a_metric_that_is_exactly_at_its_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unchanged metric records unchanged, and says nothing about retention."""
    target = tmp_path / "baseline.json"
    target.write_text(
        json.dumps(
            {
                "generated": "t",
                "note": "n",
                "metrics": {_KEY: _ALLOWANCE},
            }
        )
    )
    monkeypatch.setattr(qg, "BASELINE", target)
    qg._write_baseline([_m(_ALLOWANCE)])
    assert json.loads(target.read_text())["metrics"][_KEY] == _ALLOWANCE
    assert "ALLOWANCE KEPT" not in capsys.readouterr().out


def test_update_baseline_will_not_bank_a_regression_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the ceiling, an update still keeps the old number rather than raising.

    A red run should not be launderable into a green one by re-recording the
    baseline; the escape hatch for a genuine raise is a hand edit in a diff.
    """
    target = tmp_path / "baseline.json"
    target.write_text(
        json.dumps(
            {
                "generated": "t",
                "note": "n",
                "slack": {_KEY: _SLACK},
                "metrics": {_KEY: _ALLOWANCE},
            }
        )
    )
    monkeypatch.setattr(qg, "BASELINE", target)
    qg._write_baseline([_m(_ALLOWANCE + 10)])
    assert json.loads(target.read_text())["metrics"][_KEY] == _ALLOWANCE


def test_a_metric_with_no_prior_allowance_is_recorded_as_a_first_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shrink-only must not mean 'unmeasurable': a new metric still gets recorded."""
    target = tmp_path / "baseline.json"
    target.write_text(json.dumps({"generated": "t", "note": "n", "metrics": {}}))
    monkeypatch.setattr(qg, "BASELINE", target)
    qg._write_baseline([_m(12.0, key="brand.new")])
    assert json.loads(target.read_text())["metrics"]["brand.new"] == 12.0


def test_recorded_allowances_reports_every_number_it_declined_to_raise() -> None:
    """The retained list names each metric, its measurement and the kept number."""
    recorded, retained = qg._recorded_allowances(
        [_m(49.0), _m(7.0, key="other.metric")],
        {_KEY: _ALLOWANCE, "other.metric": 9.0},
    )
    assert recorded == {_KEY: _ALLOWANCE, "other.metric": 7.0}
    assert retained == [f"{_KEY}: measured 49, allowance kept at 48"], (
        f"exactly the raised metric must be reported, but it read {retained}"
    )


# ----- the committed table is a budget, not a hole -------------------------


def _committed() -> dict[str, dict[str, float]]:
    return json.loads(qg.BASELINE.read_text())


def test_every_committed_slack_key_is_a_real_gated_metric() -> None:
    """A slack entry for a metric nothing measures is a number nobody reads."""
    payload = _committed()
    unknown = set(payload.get("slack", {})) - set(payload["metrics"])
    assert not unknown, f"slack names metrics that carry no allowance: {sorted(unknown)}"


def test_no_hard_zero_or_report_only_metric_carries_slack() -> None:
    """A hard rule with a band is not a rule, and an ungated number cannot use one."""
    slack = _committed().get("slack", {})
    bad = (set(slack) & qg.HARD_ZERO) | (set(slack) & qg.REPORT_ONLY)
    assert not bad, (
        "if a hard-zero or report-only metric declared slack then the file "
        f"would promise headroom the gate ignores: {sorted(bad)}"
    )


def test_committed_slack_values_are_one_prs_worth_not_a_rewrite() -> None:
    """Counts get single digits, sizes get 60 lines: a PR, not a refactor."""
    slack = _committed().get("slack", {})
    metrics = _committed()["metrics"]
    for key, band in slack.items():
        assert band >= 0, f"{key}: slack must never be negative, read {band}"
        ceiling = 60.0 if key.startswith("file_size.max_") else 8.0
        assert band <= ceiling, (
            f"{key}: slack {band} is bigger than one ordinary PR's worth "
            f"({ceiling}); widen the allowance in a reviewed diff instead"
        )
        assert band <= max(metrics[key], 1.0), (
            f"{key}: slack {band} is larger than the allowance it sits on "
            f"({metrics[key]}), which is not a band, it is a new allowance"
        )


def test_ruff_total_slack_covers_its_buckets() -> None:
    """ruff.total is the sum of the five buckets, so its band must cover theirs.

    A bucket band the total cannot absorb is unusable: the PR that spends
    ruff.complexity's slack fails on ruff.total instead.
    """
    slack = _committed().get("slack", {})
    buckets = [
        "ruff.complexity",
        "ruff.coupling",
        "ruff.safety",
        "ruff.correctness",
        "ruff.style",
    ]
    assert slack.get("ruff.total", 0) >= sum(slack.get(b, 0) for b in buckets), (
        "ruff.total's slack must be at least the sum of its buckets' slack, "
        f"but it is {slack.get('ruff.total', 0)} against "
        f"{sum(slack.get(b, 0) for b in buckets)}"
    )
