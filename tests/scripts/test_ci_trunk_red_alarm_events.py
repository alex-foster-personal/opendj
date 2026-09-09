"""The trunk-red alarm must fire for the scheduled nightly, not only for pushes.

Requirements:
- [if] the nightly extended e2e tier fails on main [then] the alarm job's
  condition admits a `schedule` workflow_run, [else stop]
- [if] a pull_request run fails [then] the condition still excludes it (PR
  reds are the PR's own signal), [else stop]
- [if] the alarm watches workflows [then] both CI and E2E are listed, [else stop]
"""

from __future__ import annotations

from pathlib import Path

import yaml

ALARM = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "trunk-red-alarm.yml"


def _alarm_condition() -> str:
    doc = yaml.safe_load(ALARM.read_text())
    return " ".join(str(doc["jobs"]["trunk_red"]["if"]).split())


def test_alarm_admits_scheduled_runs_on_main() -> None:
    """[if] the nightly fails on main [then] the condition names the schedule event, [else stop]."""
    cond = _alarm_condition()
    assert "github.event.workflow_run.event == 'schedule'" in cond, cond
    assert "github.event.workflow_run.event == 'push'" in cond, cond
    assert "head_branch == github.event.repository.default_branch" in cond, cond


def test_alarm_still_excludes_pull_request_runs() -> None:
    """[if] a PR run fails [then] the alarm does not fire for it, [else stop]."""
    cond = _alarm_condition()
    assert "pull_request" not in cond, cond
    assert "conclusion == 'failure'" in cond, cond


def test_alarm_watches_both_required_workflows() -> None:
    """[if] E2E goes red on main [then] the alarm is subscribed to it, [else stop]."""
    doc = yaml.safe_load(ALARM.read_text())
    watched = (
        doc[True]["workflow_run"]["workflows"]
        if True in doc
        else doc["on"]["workflow_run"]["workflows"]
    )
    assert set(watched) >= {"CI", "E2E"}, watched
