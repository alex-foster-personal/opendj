"""Watchdog for the `window` job itself (issue #2906).

Every downstream job in periodic-checks.yml (report, skip-report,
windows-parity-report, windows-parity-skip-report) is gated on
`needs.window.result == 'success'`. That left no path to the ledger issue
(#1492) when `window` itself failed: five weeks silent, root cause fixed in
#2905 (agentbox `gh` 2.62 lacking `--slurp`), but that fix addressed one
instance, not the class -- nothing kept watch on `window` failing for any
OTHER reason.

This module asserts the watchdog job exists, fires on failure AND cancellation
(never on a state `window` cannot reach), and does not depend on any output
`window` would only have produced on success -- reading `needs.window.outputs.*`
in the failure path is the exact mistake being fixed.

-Claude
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PERIODIC = REPO_ROOT / ".github" / "workflows" / "periodic-checks.yml"


def _periodic() -> dict:
    return yaml.safe_load(PERIODIC.read_text(encoding="utf-8"))


def _alarm_job() -> dict:
    jobs = _periodic()["jobs"]
    job = jobs.get("window-failure-report")
    assert job is not None, (
        "periodic-checks.yml has no watchdog for the window job itself; a "
        "future window failure would again be invisible on the ledger issue "
        "(#1492) for as long as nobody happened to check the Actions tab"
    )
    return job


def _alarm_script() -> str:
    return "\n".join(str(s.get("run", "")) for s in _alarm_job().get("steps") or [])


def test_the_alarm_job_is_keyed_on_window_failing() -> None:
    condition = str(_alarm_job().get("if", ""))
    assert "always()" in condition, (
        "without always(), a failed `window` short-circuits this job's own "
        f"default `needs.window.result == 'success'` gate (if: {condition!r})"
    )
    assert "needs.window.result == 'failure'" in condition, (
        f"the alarm must be keyed on the window job's own failure (if: {condition!r})"
    )
    assert "window" in (_alarm_job().get("needs") or [])


def test_cancelled_is_treated_deliberately_not_silently() -> None:
    """A cancelled `window` must not read as healthy (trunk-job-verdict.yml's
    own reasoning: a cancelled run reads as neutral in every dashboard)."""
    condition = str(_alarm_job().get("if", ""))
    assert "needs.window.result == 'cancelled'" in condition, (
        "a cancelled window falls through this gate unreported, exactly the "
        f"blind spot trunk-job-verdict.yml exists to close (if: {condition!r})"
    )


def test_the_alarm_does_not_read_any_output_window_only_sets_on_success() -> None:
    """The bug being fixed: a failure path gated on the failed job's own
    outputs is not a failure path. `window` sets its outputs from the
    `compute` step, which is exactly the step that may not have run."""
    job_text = yaml.dump(_alarm_job())
    assert "needs.window.outputs" not in job_text, (
        "the watchdog reads needs.window.outputs.*, which window may never "
        f"have set if it failed before that step:\n{job_text}"
    )


def test_the_alarm_posts_to_the_ledger_issue() -> None:
    script = _alarm_script()
    assert "gh issue comment" in script
    assert "1492" in script, (
        "the watchdog must land on the periodic-checks ledger issue (#1492), "
        "the same durable channel every other row in this workflow reports to"
    )


def test_the_alarm_names_the_failure_via_the_actions_api_not_window_outputs() -> None:
    """The record must NAME the failure (acceptance criterion), derived from
    something available regardless of what `window`'s own script did."""
    script = _alarm_script()
    assert "runs/${RUN_ID}/jobs" in script or "runs/${{ github.run_id }}/jobs" in script, (
        "the watchdog should look up the failed job's own steps via the "
        "Actions API rather than guessing at what window failed on"
    )
    assert "WINDOW_RESULT" in script


def test_the_alarm_fails_the_run_visibly_too() -> None:
    """Mirrors trunk-red-alarm.yml / trunk-job-verdict.yml: the issue comment
    is the durable record, but the job also exits non-zero so the failure is
    visible on the Actions tab too, not just in the ledger."""
    script = _alarm_script()
    assert "exit 1" in script
