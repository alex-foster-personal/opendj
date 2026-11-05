"""The 30-min queued-main e2e alarm and its ONE auto-rerun (issue #5831, W46).

scripts/ci_e2e_main_starvation.py decides from the run and job listings GitHub returns.
The fixture is the recorded starving run itself: main 2411a8f23190's E2E run
37620689059, whose `e2e gate` job was created at 12:24:31Z Wed 7 Oct 2026 and was still
queued with runner_id 0 when it was read about 2h15m later. Variants copy that payload
and change one field, so every case is the real shape GitHub sends.

Regression lines:
  - if the recorded starving gate is not found then the alarm never fires on the case that
    filed #5831
  - if a gate younger than 30 min, or one with a runner, or a running one reads starved
    then the alarm cries wolf and cancels healthy runs
  - if a PR, a schedule or a non-main push run is looked at then PR e2e is cancelled
  - if attempt 1 on main's tip is not RERUN then the one auto-rerun never happens
  - if attempt 2 is RERUN then the watchdog re-runs forever
  - if a run main has moved past is re-run then an old commit takes the seat from the tip
  - if a short listing or a bad timestamp yields a verdict then UNKNOWN reads as "no starvation"
  - if the alarm body omits the job's marker then attempt-2 alarms comment every tick
  - if a failed re-run is reported as done then the promised re-run is silently lost
  - if a failed cancel or tip re-read is reported as a failed re-run then the operator re-runs
    a run that may still be live, or one main moved past
  - if state is not re-read before the cancel then a gate a runner just took is cancelled,
    or a commit main just moved past is re-run
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import ci_e2e_main_starvation as watch
from scripts.ci_e2e_main_starvation import (
    ALARM,
    CANCEL_STEP,
    RERUN,
    RERUN_STEP,
    SUPERSEDED,
    TIP_STEP,
    Failure,
    MeasurementError,
    action_for,
    find_starved,
    items_from_pages,
    outcome_text,
    recheck,
    report_body,
    rerun_or_supersede,
    starved_gate,
    verdict,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "e2e_main_starvation" / "run-37620689059.json").read_text(encoding="utf-8")
)
RUN: dict = FIXTURE["run"]
JOBS: list[dict] = FIXTURE["jobs"]["jobs"]
TIP = RUN["head_sha"]
GATE_CREATED = datetime(2026, 10, 7, 12, 24, 31, tzinfo=UTC)
#: When the coordinator's checkpoint re-judge logged `pending=e2e verdict:missing`.
REJUDGE = datetime(2026, 10, 7, 14, 4, 50, tzinfo=UTC)


def _gate(jobs: list[dict]) -> dict:
    (gate,) = [j for j in jobs if j["name"].startswith("e2e gate")]
    return gate


def _variant(run_changes: dict | None = None, gate_changes: dict | None = None):
    run = {**copy.deepcopy(RUN), **(run_changes or {})}
    jobs = copy.deepcopy(JOBS)
    _gate(jobs).update(gate_changes or {})
    return run, jobs


def _find(run: dict, jobs: list[dict], now: datetime = REJUDGE, tip: str = TIP):
    return find_starved([run], {int(run["id"]): jobs}, tip, "main", now)


def test_fixture_is_the_recorded_starving_gate() -> None:
    """if the fixture drifted from the incident then nothing below measures #5831"""
    gate = _gate(JOBS)
    assert RUN["event"] == "push" and RUN["head_branch"] == "main"
    assert gate["status"] == "queued" and not gate["runner_id"]
    assert gate["labels"] == ["self-hosted", "linux", "e2e"]
    assert watch.parse_time(gate["created_at"]) == GATE_CREATED


def test_the_recorded_gate_is_starved_and_gets_its_one_rerun() -> None:
    """if the recorded starving gate is not found then the alarm misses its own incident"""
    (item,) = _find(RUN, JOBS)
    assert item.run_id == 37620689059
    assert item.job_id == _gate(JOBS)["id"]
    assert item.queued_for == REJUDGE - GATE_CREATED
    assert item.action == RERUN


def test_threshold_is_thirty_minutes_both_ways() -> None:
    """if 29 min reads starved, or 31 min does not, then the alarm is not the 30-min alarm"""
    gate = _gate(JOBS)
    assert starved_gate(gate, GATE_CREATED + timedelta(minutes=29)) is None
    assert starved_gate(gate, GATE_CREATED + timedelta(minutes=30)) is None
    assert starved_gate(gate, GATE_CREATED + timedelta(minutes=31)) == timedelta(minutes=31)


@pytest.mark.parametrize(
    "gate_changes",
    [
        {"runner_id": 65},
        {"status": "in_progress", "runner_id": 65},
        {"status": "completed", "conclusion": "success", "runner_id": 65},
        {"status": "waiting"},
    ],
    ids=["has-runner", "running", "completed", "waiting-for-environment"],
)
def test_a_gate_with_a_runner_or_not_queued_is_not_starved(gate_changes: dict) -> None:
    """if a gate that has a runner reads starved then healthy main runs get cancelled"""
    run, jobs = _variant(gate_changes=gate_changes)
    assert _find(run, jobs) == []


def test_the_scope_job_is_never_the_subject() -> None:
    """if a non-gate job queued past 30 min trips the alarm then the subject is wrong"""
    jobs = copy.deepcopy(JOBS)
    for job in jobs:
        job.update(status="queued", runner_id=0)
    starved = _find(RUN, jobs)
    assert [s.job_name for s in starved] == [_gate(JOBS)["name"]]


@pytest.mark.parametrize(
    "run_changes",
    [
        {"event": "pull_request", "head_branch": "af--feature"},
        {"event": "pull_request", "head_branch": "main"},
        {"event": "schedule"},
        {"event": "workflow_dispatch"},
        {"event": "push", "head_branch": "af--feature"},
        {"status": "completed"},
    ],
    ids=["pr", "pr-named-main", "schedule", "dispatch", "branch-push", "completed-run"],
)
def test_only_unfinished_main_push_runs_are_looked_at(run_changes: dict) -> None:
    """if a PR, schedule or branch push run is looked at then PR e2e gets cancelled"""
    run, jobs = _variant(run_changes=run_changes)
    assert _find(run, jobs) == []


def test_in_progress_run_status_is_looked_at_too() -> None:
    """if only `queued` runs are read then a run whose scope already ran is missed"""
    run, jobs = _variant(run_changes={"status": "in_progress"})
    assert [s.action for s in _find(run, jobs)] == [RERUN]


def test_attempt_two_alarms_without_a_second_rerun() -> None:
    """if attempt 2 is RERUN then the watchdog re-runs a starving gate forever"""
    run, jobs = _variant(run_changes={"run_attempt": 2})
    assert [s.action for s in _find(run, jobs)] == [ALARM]
    assert action_for({**RUN, "run_attempt": 3}, TIP) == ALARM


def test_a_run_main_moved_past_is_cancelled_not_rerun() -> None:
    """if an old commit is re-run then it takes the seat from main's tip"""
    assert [s.action for s in _find(RUN, JOBS, tip="f" * 40)] == [SUPERSEDED]
    assert action_for({**RUN, "run_attempt": 2}, "f" * 40) == SUPERSEDED


def test_rerun_control_original_behavior_holds_on_tip_attempt_one() -> None:
    """if the overshoot (never re-run) passed the tests then R2's one re-run is unguarded"""
    assert action_for(RUN, TIP) == RERUN


@pytest.mark.parametrize("bad", [None, "", "yesterday", "2026-10-07T12:24:31"])
def test_a_bad_timestamp_is_unknown_never_young(bad: object) -> None:
    """if an unparseable created_at reads as young then UNKNOWN renders as no starvation"""
    run, jobs = _variant(gate_changes={"created_at": bad})
    with pytest.raises(MeasurementError):
        _find(run, jobs)


def test_a_short_listing_is_unknown() -> None:
    """if a listing shorter than total_count yields a verdict then a missing page hides a run"""
    with pytest.raises(MeasurementError):
        items_from_pages([{"total_count": 3, "jobs": JOBS}], "jobs", "jobs")
    with pytest.raises(MeasurementError):
        items_from_pages([], "jobs", "jobs")
    with pytest.raises(MeasurementError):
        items_from_pages([{"jobs": JOBS}], "jobs", "jobs")
    assert items_from_pages([{"total_count": len(JOBS), "jobs": JOBS}], "jobs", "jobs") == JOBS


def test_the_alarm_names_the_job_and_carries_its_dedupe_marker() -> None:
    """if the body lacks the job marker then an attempt-2 alarm comments every tick"""
    (item,) = _find(RUN, JOBS)
    body = report_body(item, REJUDGE)
    assert body.startswith(f"<!-- e2e-main-starved job={item.job_id} -->")
    assert item.job_name in body and str(item.run_id) in body and TIP in body
    assert "RERUN" in body and "100 min" in body
    assert "--variable CI_RUNS_ON_E2E_TRUNK`" in body and "CI_RUNS_ON_E2E_MAIN" not in body


def test_main_exits_unknown_when_gh_cannot_measure(tmp_path: Path) -> None:
    """if a failed listing exits 0 then the watchdog reads green while blind"""
    # A real subprocess with no gh on PATH: the measurement genuinely fails.
    import os
    import subprocess
    import sys

    env = {**os.environ, "PATH": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-m", "scripts.ci_e2e_main_starvation", "--repo", "o/r", "--dry-run"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == watch.EXIT_UNKNOWN, result.stdout + result.stderr
    assert result.stdout.startswith("[UNKNOWN]")
    assert "gh is not on PATH" in result.stdout, "gh must be resolved by path before launch"


def test_verdict_alarms_on_the_recorded_run() -> None:
    """if the recorded run exits 0 then the alarm does not fire on #5831's own case"""
    code, lines, starved = verdict([RUN], {RUN["id"]: JOBS}, TIP, "main", REJUDGE)
    assert code == watch.EXIT_ALARM and [s.action for s in starved] == [RERUN]
    assert lines[0].startswith("::error title=Main e2e gate starved::")
    assert "action RERUN" in lines[0]


def test_verdict_reads_ok_only_when_it_measured_runs() -> None:
    """if a healthy sweep prints nothing then a silent zero is indistinguishable from blind"""
    running, jobs = _variant(gate_changes={"status": "in_progress", "runner_id": 65})
    code, lines, starved = verdict([running], {running["id"]: jobs}, TIP, "main", REJUDGE)
    assert (code, starved) == (watch.EXIT_OK, [])
    assert lines == [
        "[OK] 1 unfinished main push E2E run(s) at tip 2411a8f23; no gate queued without a runner past 30 min"
    ]


def test_a_failed_rerun_is_reported_as_failed_with_the_manual_step() -> None:
    """if a failed re-run is reported as done then the promised re-run is silently lost"""
    (item,) = _find(RUN, JOBS)
    body = report_body(item, REJUDGE, Failure(RERUN_STEP, "gh api -X POST .../rerun failed: HTTP 502"))
    assert "Action: **RERUN** (FAILED at re-run)." in body
    assert "The automatic re-run FAILED" in body and "HTTP 502" in body
    assert f"gh run rerun {item.run_id}" in body
    assert "Cancelled and re-run once automatically" not in body


def test_a_failed_cancel_on_main_tip_never_says_the_rerun_failed() -> None:
    """if a failed cancel on a RERUN item reads as a failed re-run then the operator re-runs a
    run that may still be live (Codex r4208698122)"""
    (item,) = _find(RUN, JOBS)
    assert item.action == RERUN
    body = report_body(item, REJUDGE, Failure(CANCEL_STEP, "gh api -X POST .../cancel failed: HTTP 500"))
    assert "Action: **RERUN** (FAILED at cancel)." in body
    assert "The automatic cancel FAILED" in body and "Nothing was re-run" in body
    assert f"gh run cancel {item.run_id}" in body
    assert "The automatic re-run FAILED" not in body
    assert "only if `2411a8f23` is still main's tip" in body


def test_a_failed_tip_read_says_cancelled_and_not_rerun() -> None:
    """if a failed tip re-read reads as a failed re-run then an unknown tip gets re-run blindly"""
    (item,) = _find(RUN, JOBS)
    text = outcome_text(item, Failure(TIP_STEP, "could not re-read the default branch tip"))
    assert text.startswith("**Cancelled, but re-reading main's tip FAILED**")
    assert "NOT re-run" in text and "only if `2411a8f23` is still main's tip" in text
    assert "The automatic re-run FAILED" not in text


def test_a_failed_superseded_cancel_names_the_cancel_step() -> None:
    """if a failed cancel is reported as a re-run failure then the operator fixes the wrong step"""
    (item,) = _find(RUN, JOBS, tip="f" * 40)
    text = outcome_text(item, Failure(CANCEL_STEP, "timeout"))
    assert "The automatic cancel FAILED" in text and f"gh run cancel {item.run_id}" in text
    assert "rerun" not in text


def test_success_control_outcomes_are_unchanged() -> None:
    """if the overshoot (always reporting failure) passed then the success text is unguarded"""
    (rerun,) = _find(RUN, JOBS)
    assert outcome_text(rerun, None).startswith("Cancelled and re-run once automatically")
    (alarm,) = _find({**RUN, "run_attempt": 2}, JOBS)
    assert outcome_text(alarm, Failure(CANCEL_STEP, "ignored")).startswith("Alarm only")


def test_recheck_leaves_a_gate_a_runner_just_took_alone() -> None:
    """if state is not re-read before the cancel then a healthy running gate is cancelled"""
    (item,) = _find(RUN, JOBS)
    taken = {**_gate(JOBS), "status": "in_progress", "runner_id": 65}
    assert recheck(item, taken, TIP, REJUDGE) is None
    assert recheck(item, {**_gate(JOBS), "runner_id": 65}, TIP, REJUDGE) is None


def test_recheck_turns_a_rerun_into_superseded_when_main_moved() -> None:
    """if main moved after the sweep read it then the old commit must not be re-run"""
    (item,) = _find(RUN, JOBS)
    superseded = recheck(item, _gate(JOBS), "f" * 40, REJUDGE)
    assert superseded is not None
    assert superseded.action == SUPERSEDED
    moved = rerun_or_supersede(item, "f" * 40)
    assert moved.action == SUPERSEDED


def test_recheck_control_still_starved_on_the_same_tip_keeps_its_action() -> None:
    """if the overshoot (re-read always skips) passed then the re-read disables the alarm"""
    (item,) = _find(RUN, JOBS)
    assert recheck(item, _gate(JOBS), TIP, REJUDGE) == item
    assert rerun_or_supersede(item, TIP) == item


def test_a_lost_alarm_is_one_annotation_line_carrying_the_whole_alarm() -> None:
    """if an alarm whose issue write failed is not in the run log then a cancelled or re-run
    gate leaves no record at all (Codex r4208801831)"""
    (item,) = _find(RUN, JOBS)
    line = watch.lost_alarm_annotation(item, REJUDGE, None, "gh issue create failed: HTTP 500")
    assert "\n" not in line and "\r" not in line
    assert line.startswith("::error title=Main e2e gate starved; alarm issue NOT written::")
    assert "HTTP 500" in line
    assert f"<!-- e2e-main-starved job={item.job_id} -->%0A# Main e2e gate starved for a runner" in line
    assert "Cancelled and re-run once automatically" in line


def test_lost_alarm_control_keeps_the_failed_step_it_would_have_reported() -> None:
    """if the annotation dropped the act() failure then the operator loses the manual step"""
    (item,) = _find(RUN, JOBS)
    line = watch.lost_alarm_annotation(item, REJUDGE, Failure(CANCEL_STEP, "timeout"), "HTTP 500")
    assert "(FAILED at cancel)" in line and f"gh run cancel {item.run_id}" in line


def test_an_attempt_two_alarm_says_the_run_stays_queued_and_how_to_cancel_it() -> None:
    """if the ALARM body is silent on the run it left queued then trunk stays blocked unnoticed"""
    (alarm,) = _find({**RUN, "run_attempt": 2}, JOBS)
    text = outcome_text(alarm, None)
    assert "NOT cancelled" in text and f"gh run cancel {alarm.run_id}" in text
    assert "push-tip" in text


class _FakeGh:
    """Records act()'s side effects in order, standing in for the gh helpers."""

    def __init__(self, tips: list[str], job: dict | None = None) -> None:
        self.calls: list[str] = []
        self.tips = list(tips)
        self.job = job if job is not None else _gate(JOBS)
        self.fail: dict[str, MeasurementError] = {}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(watch, "RETRY_DELAYS_S", (0,))
        monkeypatch.setattr(watch, "_job", self._job)
        monkeypatch.setattr(watch, "_tip", self._tip)
        monkeypatch.setattr(watch, "_cancel_and_wait", lambda repo, run_id: self._do(f"cancel {run_id}"))
        monkeypatch.setattr(watch, "_rerun", lambda repo, run_id: self._do(f"rerun {run_id}"))
        monkeypatch.setattr(
            watch,
            "_alarm",
            lambda repo, item, now, failure: self._do(
                f"alarm {item.action}{f' FAILED {failure.step}' if failure else ''}"
            ),
        )

    def _do(self, call: str) -> None:
        self.calls.append(call)
        name = call.split(maxsplit=1)[0]
        if name in self.fail:
            raise self.fail[name]

    def _job(self, repo: str, job_id: int) -> dict:
        self.calls.append(f"job {job_id}")
        return self.job

    def _tip(self, repo: str) -> str:
        self.calls.append("tip")
        return self.tips.pop(0)


def test_act_rereads_then_cancels_rereads_tip_reruns_and_only_then_alarms(monkeypatch) -> None:
    """if the alarm is written before the re-run, or the tip is not re-read before it, then the
    alarm claims a step that never happened or an old commit gets re-run"""
    (item,) = _find(RUN, JOBS)
    fake = _FakeGh([TIP, TIP])
    fake.install(monkeypatch)
    acted, failure = watch.act("o/r", item)
    assert failure is None and acted is not None and acted.action == RERUN
    assert fake.calls == [
        f"job {item.job_id}",
        "tip",
        f"cancel {item.run_id}",
        "tip",
        f"rerun {item.run_id}",
        "alarm RERUN",
    ]


def test_act_does_not_rerun_when_main_moves_between_cancel_and_rerun(monkeypatch) -> None:
    """if the tip is not re-read after the cancel then a commit main moved past is re-run"""
    (item,) = _find(RUN, JOBS)
    fake = _FakeGh([TIP, "f" * 40])
    fake.install(monkeypatch)
    acted, failure = watch.act("o/r", item)
    assert failure is None and acted is not None and acted.action == SUPERSEDED
    assert f"rerun {item.run_id}" not in fake.calls and fake.calls[-1] == "alarm SUPERSEDED"


def test_act_on_attempt_two_alarms_without_cancelling(monkeypatch) -> None:
    """if attempt 2 is cancelled or re-run then the one-rerun budget is not enforced at act()"""
    (item,) = _find({**RUN, "run_attempt": 2}, JOBS)
    fake = _FakeGh([TIP])
    fake.install(monkeypatch)
    watch.act("o/r", item)
    assert fake.calls == [f"job {item.job_id}", "tip", "alarm ALARM"]


def test_act_alarms_with_the_failed_cancel_and_never_reruns(monkeypatch) -> None:
    """if a failed cancel still re-runs, or is not in the alarm, then a live run gets doubled"""
    (item,) = _find(RUN, JOBS)
    fake = _FakeGh([TIP, TIP])
    fake.fail["cancel"] = MeasurementError("HTTP 500")
    fake.install(monkeypatch)
    _acted, failure = watch.act("o/r", item)
    assert failure == Failure(CANCEL_STEP, "HTTP 500")
    assert fake.calls[-1] == "alarm RERUN FAILED cancel"
    assert not any(c.startswith("rerun") for c in fake.calls)


def test_act_leaves_a_gate_a_runner_took_on_reread_alone(monkeypatch) -> None:
    """if act() trusts the sweep's stale listing then a gate that just got a runner is cancelled"""
    (item,) = _find(RUN, JOBS)
    fake = _FakeGh([TIP], job={**_gate(JOBS), "status": "in_progress", "runner_id": 65})
    fake.install(monkeypatch)
    assert watch.act("o/r", item) == (None, None)
    assert fake.calls == [f"job {item.job_id}", "tip"]


def test_already_reported_finds_the_job_marker_in_comments_or_body(monkeypatch) -> None:
    """if the dedupe misses its own marker then an attempt-2 alarm comments on every sweep"""
    marker = watch.MARKER.format(job_id=42)

    def fake_json(comments: list[list[dict]], body: str):
        def _gh_json(args: list[str]) -> object:
            return comments if "--paginate" in args else {"body": body}

        return _gh_json

    monkeypatch.setattr(watch, "_gh_json", fake_json([[{"body": "x"}, {"body": f"{marker}\nold"}]], ""))
    assert watch._already_reported("o/r", 7, 42)
    monkeypatch.setattr(watch, "_gh_json", fake_json([[{"body": "x"}]], marker))
    assert watch._already_reported("o/r", 7, 42)
    monkeypatch.setattr(watch, "_gh_json", fake_json([[{"body": watch.MARKER.format(job_id=421)}]], ""))
    assert not watch._already_reported("o/r", 7, 42)


def test_one_failed_act_does_not_stop_the_sweep_but_still_exits_unknown(monkeypatch) -> None:
    """if the first starved run's failure ends the sweep then the other starved runs sit queued"""
    (first,) = _find(RUN, JOBS)
    second = watch.replace(first, run_id=first.run_id + 1, job_id=first.job_id + 1)
    monkeypatch.setattr(watch, "measure", lambda repo: ([], {}, TIP, "main"))
    monkeypatch.setattr(watch, "verdict", lambda *a: (watch.EXIT_ALARM, [], [first, second]))
    acted: list[int] = []

    def fake_act(repo: str, item):
        acted.append(item.run_id)
        if item is first:
            raise MeasurementError("cancel wait timed out")
        return item, None

    monkeypatch.setattr(watch, "act", fake_act)
    assert watch.main(["--repo", "o/r"]) == watch.EXIT_UNKNOWN
    assert acted == [first.run_id, second.run_id]


def test_sweep_control_all_acts_succeed_keeps_the_alarm_exit(monkeypatch) -> None:
    """if the overshoot (always UNKNOWN after acting) passed then a clean sweep reads blind"""
    (first,) = _find(RUN, JOBS)
    monkeypatch.setattr(watch, "measure", lambda repo: ([], {}, TIP, "main"))
    monkeypatch.setattr(watch, "verdict", lambda *a: (watch.EXIT_ALARM, [], [first]))
    monkeypatch.setattr(watch, "act", lambda repo, item: (item, None))
    assert watch.main(["--repo", "o/r"]) == watch.EXIT_ALARM
