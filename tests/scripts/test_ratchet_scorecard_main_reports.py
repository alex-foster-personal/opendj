"""scripts/ratchet_scorecard.py: gate keys, classes, fix kinds, scoring and verdicts.

The quality line and the pytest summary line are copied from real ci.yml job logs
(quality job 116355559982 and a pytest fast lane shard, Thu 15 Oct 2026).
"""

from __future__ import annotations

import datetime as dt
from collections import Counter

from scripts import ratchet_scorecard as rs
from tests.scripts.ratchet_scorecard_helpers import (
    QUALITY_LOG,
    T0,
    _actions_replay,
    _complete,
    _gate,
    _head,
    _job,
    _main_api,
)


def test_a_cancelled_main_push_is_a_report_gap_until_a_later_push_reports() -> None:
    """A main push has a per-sha concurrency group, so nothing reruns a cancelled one; its
    report is missing. The quality gate measures main's whole tree, so a later main push
    that reported covers it. Captured: run 38889649657 at 92b5dd929, cancelled, then run
    38889676198 at 9cbfb6527, created 15 s later, which logged its report (Fri 16 Oct 2026)."""
    since = dt.datetime(2026, 10, 16, 10, 0, tzinfo=dt.UTC)
    window = (since, since + dt.timedelta(hours=1))
    covered = rs.collect(
        _actions_replay("main-cancelled"), since, since + dt.timedelta(hours=2), workers=1, main_window=window
    )
    assert (covered.unreadable_logs, covered.main_unread_logs) == (0, 0)
    # A later push covers only if its own report was read: with its log lost it covers
    # nothing. It finished after the scored window, so its own lost log is that window's.
    lost = rs.collect(
        _actions_replay("main-cancelled", lost={116936195809}),
        since,
        since + dt.timedelta(hours=2),
        workers=1,
        main_window=window,
    )
    assert (lost.unreadable_logs, lost.main_unread_logs) == (1, 1)
    # Read before the later push finished (11:36:45Z), nothing has reported after the
    # cancelled one yet, so the window cannot be measured.
    early = rs.collect(
        _actions_replay("main-cancelled"), since, since + dt.timedelta(hours=1), workers=1, main_window=window
    )
    assert (early.unreadable_logs, early.main_unread_logs) == (1, 1)
    assert rs.unmeasured_window(early) is not None


def test_an_unread_main_report_log_is_counted_unreadable() -> None:
    def api(path: str) -> str:
        if path.endswith("/logs"):
            raise RuntimeError("gh: HTTP 410")
        return _main_api([])(path)

    since, until = dt.datetime(2026, 10, 16, 11, 0, tzinfo=dt.UTC), dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.UTC)
    data = rs.collect(api, since, until, workers=1)
    assert (dict(data.main_red), data.unreadable_logs) == ({}, 1)
    assert data.main_unread_logs == 1


def test_a_passing_e2e_main_push_is_no_report_gap() -> None:
    """e2e.yml has no quality job and a passing main push of it is never read, so its jobs
    are not a missing quality report. The fixture holds the real e2e run of the same push."""
    calls: list[str] = []
    since = dt.datetime(2026, 10, 16, 11, 0, tzinfo=dt.UTC)
    data = rs.collect(_main_api(calls), since, since + dt.timedelta(hours=1), workers=1)
    assert (data.main_unread_logs, data.unreadable_logs) == (0, 0)
    assert dict(data.main_red) == {"quality:mypy.errors_tests": 1}
    assert not any("/runs/38894149533/" in c for c in calls)


def test_an_unread_main_log_fails_a_window_that_has_a_complete_pr_head() -> None:
    """Main's report-only log is the evidence a demoted gate is monitored by, so a window
    that lost one is unmeasured even when a PR head beside it is complete."""
    heads = _complete([_head("a", "s1", 1)])
    lost = rs.Collected(heads, Counter(), 1, 1, 1, main_unread_logs=1)
    assert "main" in (rs.unmeasured_window(lost) or "")
    # Control: the same window with main's log read is scored.
    assert rs.unmeasured_window(rs.Collected(heads, Counter(), 1, 0, 1)) is None


def test_a_main_quality_job_that_never_reported_is_a_report_gap() -> None:
    """A main quality job that failed before its measuring step (setup, mypy cache) wrote
    no report, so main's silence on every gate is not evidence they are clean."""
    step = rs.MEASURING_STEPS[rs.QUALITY_JOB]

    def quality(conclusion: str, step_conclusion: str | None) -> dict:
        steps = [{"name": "Set up job", "conclusion": "failure" if step_conclusion is None else "success"}]
        if step_conclusion is not None:
            steps.append({"name": step, "conclusion": step_conclusion})
        return {**_job(f"{rs.QUALITY_JOB} (lint debt)", conclusion, 1), "steps": steps}

    assert rs._main_report_gaps([quality("failure", None)], frozenset()) == 1
    assert rs._main_report_gaps([quality("failure", "skipped")], frozenset()) == 1
    assert rs._main_report_gaps([quality("success", "skipped")], frozenset()) == 1
    # A report that left a metric UNKNOWN is a gap too.
    assert rs._main_report_gaps([quality("failure", "failure")], frozenset({"quality-unknown:x.y"})) == 1
    # Controls: a report that ran, red or green, is evidence; a cancelled run is superseded.
    assert rs._main_report_gaps([quality("failure", "failure")], frozenset({"quality:x.y"})) == 0
    assert rs._main_report_gaps([quality("success", "success")], frozenset()) == 0
    assert rs._main_report_gaps([quality("cancelled", "cancelled")], frozenset()) == 0
    # A run with no quality job measured nothing (startup_failure, action_required).
    assert rs._main_report_gaps([{**_job("contracts", "failure", 2), "steps": []}], frozenset()) == 1
    assert rs._main_report_gaps([], frozenset()) == 1
    # Only a pull_request can come out of scope, so a skipped main quality job is always a
    # gap, whether scope failed or passed.
    skipped = {**_job(f"{rs.QUALITY_JOB} (lint debt)", "skipped", 3), "steps": []}
    scope = {**_job("ci scope (docs-only skip decision)", "failure", 4), "steps": []}
    assert rs._main_report_gaps([scope, skipped], frozenset()) == 1
    assert rs._main_report_gaps([{**scope, "conclusion": "success"}, skipped], frozenset()) == 1


def test_an_unread_log_on_a_fix_horizon_head_makes_the_fix_unreadable() -> None:
    """A head past the window, consulted only as the fix, with an unread log (a selection
    report read as "ran nothing") may be where the red cleared, so its fix is unreadable."""
    import dataclasses

    end = T0 + dt.timedelta(hours=10)
    red = _head("a", "s1", 1, "quality:x")
    blind = dataclasses.replace(_head("a", "s2", 12), log_jobs=1, unread_logs=1)
    green = _head("a", "s3", 14)
    files = lambda red_sha, fix_sha: ["(private path)"]  # noqa: E731
    gate = rs.score(_complete([red, blind, green]), Counter(), files, end, (T0, end), rs.LOOKAHEAD)["quality:x"]
    assert dict(gate.fixes) == {"unreadable": 1} and gate.unresolved == 0
    # Control: read, the same head is the fix; and an unread head after the fix is not consulted.
    read = dataclasses.replace(blind, unread_logs=0)
    gate = rs.score(_complete([red, read, green]), Counter(), files, end, (T0, end), rs.LOOKAHEAD)["quality:x"]
    assert gate.fixes["unreadable"] == 0 and sum(gate.fixes.values()) == 1
    late = dataclasses.replace(green, log_jobs=1, unread_logs=1)
    gate = rs.score(_complete([red, read, late]), Counter(), files, end, (T0, end), rs.LOOKAHEAD)["quality:x"]
    assert gate.fixes["unreadable"] == 0 and sum(gate.fixes.values()) == 1


def test_a_failed_aggregate_with_every_gating_job_green_is_unmeasured() -> None:
    """`ci gate` failing on its own checkout or evaluator, beside green gating jobs, is no
    verdict: read as green it could clear an earlier red."""
    green = {**_job("quality ratchet (lint debt)", "success", 1), "steps": []}
    gate = {**_job("ci gate", "failure", 2), "steps": []}
    assert rs.judge_jobs([green, gate], {}) == (frozenset(), False, 0, frozenset())
    # Controls: the aggregate passing is a measured green; failing beside a named red is that red.
    assert rs.judge_jobs([green, {**gate, "conclusion": "success"}], {})[1] is True
    red = {**_job("quality ratchet (lint debt)", "failure", 3), "steps": []}
    assert rs.judge_jobs([red, gate], {3: "[quality] REGRESSION        x.y: 2 > 1\n"}) == (
        frozenset({"quality:x.y"}),
        True,
        0,
        frozenset(),
    )


def test_each_gate_key_names_the_job_it_comes_from() -> None:
    assert rs.origin_job("quality:frontend.max_fan_in") == rs.QUALITY_JOB
    assert rs.origin_job("pytest:tests/webui/test_x.py") == "pytest fast lane"
    assert rs.origin_job("contracts: openapi drift") == "contracts"


def test_an_unmeasured_red_head_in_the_window_blocks_every_demotion() -> None:
    """A head whose verdict was unmeasured (a quality UNKNOWN beside a REGRESSION, a
    timed-out e2e) drops out of every count, so readable paperwork fixes beside it are not
    evidence for demoting anything."""
    measured = _complete([_head("a", "s1", 1, "quality:x"), _head("a", "s2", 2)])
    partial = _complete([_head("b", "t1", 1, "quality:x", measured=False)])
    # A superseded head (its e2e run cancelled, so missing, under a newer push) and a queue
    # draft are not counted.
    superseded = [_head("c", "u1", 1, "quality:x"), *_complete([_head("c", "u2", 2)])]
    draft = _complete([_head("trunk-merge/1", "d1", 1, "quality:x", measured=False)])
    end = T0 + dt.timedelta(hours=10)
    window = (T0, end)
    assert rs.unmeasured_heads(measured + partial, window) == 1
    assert rs.unmeasured_heads(measured + superseded + draft, window) == 0
    # The newest sha with a workflow not yet run (e2e still queued) is partial, not clean.
    assert rs.unmeasured_heads([_head("f", "w1", 1)], window) == 1
    assert rs.unmeasured_heads(_complete([_head("f", "w1", 1)]), window) == 0  # control
    # A timed-out job names no gate and may hide any, so a keyless unmeasured head counts too.
    assert rs.unmeasured_heads(_complete([_head("e", "v1", 1, measured=False)]), window) == 1
    # Outside the window it belongs to another day's scorecard.
    assert rs.unmeasured_heads(partial, (end, end + dt.timedelta(days=1))) == 0
    scores = rs.score(measured + partial, {}, lambda red, fix: ["docs/x.md"], end, window=window)
    assert scores["quality:x"].blind_heads == 1
    gate = _gate("ratchet", 9, sole=1, paperwork=3)
    assert gate.verdict()[0] == "AFTER-MERGE"  # control
    gate.blind_heads = 1
    assert gate.verdict() == (
        "UNKNOWN",
        "1 unread PR head(s) in the window could have moved this gate: its counts are incomplete",
    )


def test_a_gate_untouched_by_the_unread_head_still_scores() -> None:
    """Scoping is per gate: an unread head on a branch red on `quality:x` must not stop a gate
    that only another branch was red on from reaching its ordinary verdict."""
    window = (T0, T0 + dt.timedelta(hours=10))
    end = window[1]
    quiet = _complete([_head("a", "s1", 1, "quality:y"), _head("a", "s2", 2)])
    unread = _complete([_head("b", "t1", 1, "quality:x", measured=False)])
    heads = quiet + unread
    assert rs.unread_scopes(heads, window) == {"quality:x": 1}
    scores = rs.score(heads, {}, lambda red, fix: ["docs/x.md"], end, window=window)
    assert scores["quality:x"].blind_heads == 1
    assert scores["quality:x"].verdict()[0] == "UNKNOWN"
    # `quality:y` was never red on branch b, so the unread head cannot move it.
    assert scores["quality:y"].blind_heads == 0
    assert scores["quality:y"].verdict()[0] == "QUIET"


def test_ignoring_the_unread_head_would_demote_its_branch_gate() -> None:
    """Control for the scoping: with the same counts but no unread head in scope, this gate
    reaches WARN. So UNKNOWN is what holds the demotion, not an unrelated quiet verdict."""
    gate = _gate("ratchet", 3, sole=1, paperwork=1, tests=2)
    assert gate.verdict()[0] == "WARN"  # control: readable evidence alone would demote
    gate.blind_heads = 1
    assert gate.verdict()[0] == "UNKNOWN"


def test_a_head_whose_only_red_was_lost_scopes_the_gate_it_could_have_moved() -> None:
    """The P1: judge_jobs dropped the gate key an unread log hid, so a branch with no other
    red named nothing and scoped nothing. That gate was then scored from the other branches
    alone and reached AFTER-MERGE, a demotion recommendation, with a head's evidence missing.
    The failed job's name is what survives the lost log, and it says which gate was in play."""
    end = T0 + dt.timedelta(days=1)
    hidden = [
        rs.Head("ci.yml", "b_hidden", "h1", T0, frozenset(), False, log_jobs=1, unread_logs=1),
        rs.Head("e2e.yml", "b_hidden", "h1", T0, frozenset()),
    ]
    branches = _complete([h for b in "abc" for h in (_head(b, f"r{b}", 1, "quality:complexity"), _head(b, f"c{b}", 3))])
    gate = rs.score(branches + hidden, {}, lambda red, fix: [".planning/x.md"], end)["quality:complexity"]
    assert (len(gate.branches), gate.fixes["paperwork"], gate.blind_heads) == (3, 3, 1)
    assert gate.verdict()[0] == "UNKNOWN"
    # Control: read that head's log and there is no lost evidence, so the demotion stands.
    read = [rs.Head(h.workflow, h.branch, h.sha, h.at, h.red) for h in hidden]
    control = rs.score(branches + read, {}, lambda red, fix: [".planning/x.md"], end)["quality:complexity"]
    assert (control.blind_heads, control.verdict()[0]) == (0, "AFTER-MERGE")


def test_a_lost_log_still_names_the_job_that_could_have_carried_the_red() -> None:
    """The evidence that survives an unread log is its job's name: the quality ratchet can
    name any metric, a pytest shard any module, so the name is the gate family to scope."""
    quality = {**_job(rs.QUALITY_JOB, "failure", 7), "steps": [{"name": "Quality ratchet", "conclusion": "failure"}]}
    assert rs.judge_jobs([quality], {}) == (frozenset(), False, 1, frozenset({rs.QUALITY_JOB}))
    shard = {**_job("pytest fast lane (shard 2 of 5)", "failure", 8), "steps": []}
    assert rs.judge_jobs([shard], {})[3] == frozenset({"pytest fast lane"})
    # Control: a readable log names its own key and leaves no family to scope.
    assert rs.judge_jobs([{**quality, "id": 9}], {9: QUALITY_LOG})[3] == frozenset()


def test_a_job_that_ended_without_a_verdict_still_scopes_its_gate_family() -> None:
    """The P1: a job that timed out or was cancelled never printed a verdict, so judge_jobs
    left the run unmeasured but named no job, and the gate family that job could have been red
    on stayed scored from the other branches alone. Three paperwork fixes then read AFTER-MERGE
    on evidence that never existed. The surviving name is the job's, so it scopes what
    _job_covers says it can name: pytest and quality jobs a whole family, any other gating job
    only its own `<job>: <step>` keys, so an unrelated timeout cannot void the card."""
    for conclusion in ("timed_out", "cancelled", "stale"):
        assert rs.judge_jobs([_job(rs.QUALITY_JOB, conclusion, 7)], {}) == (
            frozenset(),
            False,
            0,
            frozenset({rs.QUALITY_JOB}),
        )
        assert rs.judge_jobs([_job("pytest fast lane (shard 2 of 5)", conclusion, 8)], {})[3] == frozenset(
            {"pytest fast lane"}
        )
    # A generic job keeps its name too: its ratchet steps are gates of the form `<job>: <step>`.
    frontend = "frontend unit + check + build"
    assert rs.judge_jobs([_job(frontend, "timed_out", 9)], {})[3] == frozenset({frontend})

    window = (T0, T0 + dt.timedelta(hours=10))
    lost = [
        rs.Head("ci.yml", "b_lost", "t1", T0, frozenset(), False, unread_jobs=frozenset({frontend})),
        rs.Head("e2e.yml", "b_lost", "t1", T0, frozenset()),
    ]
    blind = rs.unread_gate_scope(lost, window)
    assert (blind(f"{frontend}: Bundle budgets"), blind("quality:complexity")) == (1, 0)

    end = T0 + dt.timedelta(days=1)
    branches = _complete([h for b in "abc" for h in (_head(b, f"r{b}", 1, "quality:complexity"), _head(b, f"c{b}", 3))])
    _, measured, _, unread = rs.judge_jobs([_job(rs.QUALITY_JOB, "timed_out", 10)], {})
    hidden = [
        rs.Head("ci.yml", "b_hidden", "h1", T0, frozenset(), measured, unread_jobs=frozenset(unread)),
        rs.Head("e2e.yml", "b_hidden", "h1", T0, frozenset()),
    ]
    gate = rs.score(branches + hidden, {}, lambda red, fix: [".planning/x.md"], end)["quality:complexity"]
    assert (gate.fixes["paperwork"], gate.blind_heads) == (3, 1)
    assert gate.verdict()[0] == "UNKNOWN"


def test_a_queue_draft_withholds_no_family_though_its_aggregate_failed() -> None:
    """A draft history is excluded from scoring everywhere else (`_unread_shas`, head
    summaries, the verdict loop), so the measured-head withholding loop must skip it too:
    otherwise a gate reads UNKNOWN only because a trunk-merge draft skipped a job."""

    def history(branch: str) -> list[rs.Head]:
        return [
            rs.Head(
                "ci.yml",
                branch,
                "d1",
                T0,
                frozenset({"quality:x"}),
                skipped=frozenset({"quality ratchet"}),
                aggregate=rs.AGGREGATE_FAILURE,
                draft=branch.startswith("trunk-merge/"),
            ),
            rs.Head("e2e.yml", branch, "d1", T0, frozenset()),
        ]

    assert rs.unread_gate_scope(history("b_pr"), None)("quality:complexity") == 1
    draft = rs.unread_gate_scope(history("trunk-merge/pr-1/x"), None)
    assert (draft("quality:complexity"), draft("quality:x")) == (0, 0)


def test_a_parsed_unmeasured_job_still_scopes_the_family_its_key_cannot_name() -> None:
    """The P1: a quality log with no REGRESSION/UNKNOWN line, or a shard killed before it
    named a test, mints an `unmeasured` key and leaves the run unmeasured -- but named no job,
    so nothing scoped the gate family it could have failed. A metric three other branches
    fixed with paperwork then read AFTER-MERGE on a head whose verdict was missing."""
    quality = {**_job(rs.QUALITY_JOB, "failure", 7), "steps": [{"name": "Install deps", "conclusion": "failure"}]}
    assert rs.judge_jobs([quality], {7: "setup noise, no metric line\n"}) == (
        frozenset({"quality-unknown:Install deps"}),
        False,
        0,
        frozenset({rs.QUALITY_JOB}),
    )
    shard = {**_job("pytest fast lane (shard 2 of 5)", "failure", 8), "steps": []}
    assert rs.judge_jobs([shard], {8: "killed before any FAILED line\n"}) == (
        frozenset({"pytest:<no identity>"}),
        False,
        0,
        frozenset({"pytest fast lane"}),
    )
    # Control: a readable log names its own key and leaves no family to scope.
    assert rs.judge_jobs([{**quality, "id": 9}], {9: QUALITY_LOG})[3] == frozenset()

    end = T0 + dt.timedelta(days=1)
    paperwork = lambda red, fix: [".planning/x.md"]  # noqa: E731 - the fixture's shape
    for family_key, lost_job, log, log_id in (
        ("quality:complexity", rs.QUALITY_JOB, "setup noise, no metric line\n", 10),
        ("pytest:tests/quality/test_x.py", "pytest fast lane", "killed before any FAILED line\n", 11),
    ):
        branches = _complete([h for b in "abc" for h in (_head(b, f"r{b}", 1, family_key), _head(b, f"c{b}", 3))])
        red, measured, _, unread = rs.judge_jobs([_job(lost_job, "failure", log_id)], {log_id: log})
        hidden = [
            rs.Head("ci.yml", "b_hidden", "h1", T0, red, measured, unread_jobs=unread),
            rs.Head("e2e.yml", "b_hidden", "h1", T0, frozenset()),
        ]
        gate = rs.score(branches + hidden, {}, paperwork, end)[family_key]
        assert (gate.fixes["paperwork"], gate.blind_heads) == (3, 1), family_key
        assert gate.verdict()[0] == "UNKNOWN", family_key
        # Control: with the job's evidence read, the same heads reach the demotion.
        read = [rs.Head("ci.yml", "b_hidden", "h1", T0, frozenset()), hidden[1]]
        control = rs.score(branches + read, {}, paperwork, end)[family_key]
        assert (control.blind_heads, control.verdict()[0]) == (0, "AFTER-MERGE"), family_key


def test_a_sha_missing_a_whole_workflow_scopes_the_gates_that_workflow_carries() -> None:
    """The P1: a newest sha with an e2e.yml run and no ci.yml run yet is correctly unread,
    but it named no job and lost no log, so it scoped nothing and a quality metric three
    other branches fixed with paperwork read AFTER-MERGE. The absent workflow could have
    failed any gate it declares, so its families are scoped; the workflow that did run is
    not, so one queued workflow cannot void the card."""
    end = T0 + dt.timedelta(days=1)
    branches = _complete([h for b in "abc" for h in (_head(b, f"r{b}", 1, "quality:complexity"), _head(b, f"c{b}", 3))])
    queued = [rs.Head("e2e.yml", "b_queued", "q1", T0, frozenset())]
    blind = rs.unread_gate_scope(branches + queued, None)
    assert blind("quality:complexity") == 1
    assert blind("pytest:tests/quality/test_x.py") == 1
    assert blind("reqs-check + native wheel + contract drift: Contract drift") == 1
    # Control: the workflow that did run names no missing family.
    assert blind("e2e gate: Hermetic gate - stretch quality smoke") == 0
    gate = rs.score(branches + queued, {}, lambda red, fix: [".planning/x.md"], end)["quality:complexity"]
    assert (gate.fixes["paperwork"], gate.blind_heads) == (3, 1)
    assert gate.verdict()[0] == "UNKNOWN"
    # Control: with the ci.yml run present and measured, nothing is missing and it demotes.
    complete = queued + [rs.Head("ci.yml", "b_queued", "q1", T0, frozenset())]
    control = rs.score(branches + complete, {}, lambda red, fix: [".planning/x.md"], end)["quality:complexity"]
    assert (control.blind_heads, control.verdict()[0]) == (0, "AFTER-MERGE")


def test_a_run_that_collected_no_jobs_scopes_its_workflow_gates() -> None:
    """A run that ended (cancelled, startup failure) with no job at all is not a green head:
    judge_jobs reads an empty job list as measured, and run_gave_verdict refuses it. Nothing
    inside it can be named, so it scopes every gate family its workflow declares."""
    assert rs.judge_jobs([], {}) == (frozenset(), True, 0, frozenset())
    assert not rs.run_gave_verdict("failure", [])
    end = T0 + dt.timedelta(days=1)
    branches = _complete([h for b in "abc" for h in (_head(b, f"r{b}", 1, "quality:complexity"), _head(b, f"c{b}", 3))])
    empty = [
        rs.Head("ci.yml", "b_empty", "e1", T0, frozenset(), False, zero_jobs=True),
        rs.Head("e2e.yml", "b_empty", "e1", T0, frozenset()),
    ]
    gate = rs.score(branches + empty, {}, lambda red, fix: [".planning/x.md"], end)["quality:complexity"]
    assert (gate.blind_heads, gate.verdict()[0]) == (1, "UNKNOWN")
    # Only a run whose jobs were READ and empty may claim that. A run nothing read (cancelled,
    # or a passing one on a branch with no red) is not evidence it had no job.
    assert rs.read_zero_jobs({"id": 1}, {1: []})
    assert not rs.read_zero_jobs({"id": 1}, {1: [_job("quality ratchet", "failure")]})
    assert not rs.read_zero_jobs({"id": 2}, {1: []})


def test_an_unsuperseded_cancelled_run_scopes_the_gates_its_workflow_carries() -> None:
    """Codex P1 at d0e54c13f7: a newest sha with a cancelled ci.yml and a completed e2e.yml is
    unread, but runs_to_read never fetches the cancelled run's jobs, so it is not zero_jobs and
    the workflow is present, so the missing-workflow rule did not fire either: the head scoped
    no family and three paperwork fixes on other branches demoted a quality gate. A cancellation
    nothing superseded gave no verdict, so every gate its workflow declares is unmeasured."""
    end = T0 + dt.timedelta(days=1)
    branches = _complete([h for b in "abc" for h in (_head(b, f"r{b}", 1, "quality:complexity"), _head(b, f"c{b}", 3))])
    cancelled = [
        rs.Head("ci.yml", "b_cancelled", "c1", T0, frozenset(), False, cancelled=True),
        rs.Head("e2e.yml", "b_cancelled", "c1", T0, frozenset()),
    ]
    blind = rs.unread_gate_scope(branches + cancelled, None)
    assert blind("quality:complexity") == 1
    assert blind("pytest:tests/quality/test_x.py") == 1
    # The workflow that did conclude scopes no family of its own.
    assert blind("e2e gate: Hermetic gate - stretch quality smoke") == 0
    gate = rs.score(branches + cancelled, {}, lambda red, fix: [".planning/x.md"], end)["quality:complexity"]
    assert (gate.fixes["paperwork"], gate.blind_heads, gate.verdict()[0]) == (3, 1, "UNKNOWN")
    # Control: a cancellation a newer push superseded is no evidence either way, so it scopes
    # nothing and the same three branches reach their demotion.
    superseded = [
        rs.Head("ci.yml", "b_cancelled", "c1", T0, frozenset(), False, cancelled=True, created=T0),
        rs.Head("e2e.yml", "b_cancelled", "c1", T0, frozenset(), created=T0),
        *[
            rs.Head(w, "b_cancelled", "c2", T0 + dt.timedelta(hours=2), frozenset(), created=T0 + dt.timedelta(hours=2))
            for w in rs.WORKFLOWS
        ],
    ]
    control = rs.score(branches + superseded, {}, lambda red, fix: [".planning/x.md"], end)["quality:complexity"]
    assert (control.blind_heads, control.verdict()[0]) == (0, "AFTER-MERGE")


def test_an_aggregate_only_failure_scopes_the_gates_its_skipped_jobs_carry() -> None:
    """Codex P1 at b54f875a76a: `ci gate` fails by design when a gating job was unexpectedly
    skipped, which leaves the run unmeasured with no named red at all, and the skipped job was
    kept only in Head.skipped. The quality family was therefore scoped by nothing and three
    paperwork fixes on other branches demoted it. A skipped gating job measures nothing, so what
    it could have named is scoped -- while a measured run's docs-only skip stays a decision."""
    skipped = rs.skipped_jobs([_job(rs.QUALITY_JOB, "skipped"), _job("ci gate", "failure")])
    assert skipped == frozenset({rs.QUALITY_JOB})
    red, measured, _, unread = rs.judge_jobs([_job(rs.QUALITY_JOB, "skipped"), _job("ci gate", "failure")], {})
    assert (red, measured, unread) == (frozenset(), False, frozenset())
    end = T0 + dt.timedelta(days=1)
    branches = _complete([h for b in "abc" for h in (_head(b, f"r{b}", 1, "quality:complexity"), _head(b, f"c{b}", 3))])
    aggregate = [
        rs.Head("ci.yml", "b_skipped", "k1", T0, red, measured, skipped=skipped),
        rs.Head("e2e.yml", "b_skipped", "k1", T0, frozenset()),
    ]
    gate = rs.score(branches + aggregate, {}, lambda red, fix: [".planning/x.md"], end)["quality:complexity"]
    assert (gate.fixes["paperwork"], gate.blind_heads, gate.verdict()[0]) == (3, 1, "UNKNOWN")
    # Control: a measured run's skipped quality job (docs-only) passed nothing to clear, but it
    # is a decision, not missing evidence, so it scopes no family.
    docs_only = [
        rs.Head(
            "ci.yml",
            "b_skipped",
            "k1",
            T0,
            frozenset(),
            True,
            skipped=frozenset({rs.QUALITY_JOB}),
            aggregate=rs.AGGREGATE_SUCCESS,
        ),
        rs.Head("e2e.yml", "b_skipped", "k1", T0, frozenset()),
    ]
    control = rs.score(branches + docs_only, {}, lambda red, fix: [".planning/x.md"], end)["quality:complexity"]
    assert (control.blind_heads, control.verdict()[0]) == (0, "AFTER-MERGE")
