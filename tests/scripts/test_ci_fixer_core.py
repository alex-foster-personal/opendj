"""Tests for scripts.ci_fixer_core: dedupe ledger, known-unfixable detection,
guard-rail path checks, comment rendering, and KPI derivation.

Acceptance criteria (issue #1017):
- [if] the same run_id+job_id is seen twice then the second is deduped, never a
  second proposal [else broken].
- [if] a failure carries #1029's orphaned-provenance-SHA signature then the lane
  classifies it known-unfixable and declines rather than inventing a SHA [else
  broken -- this is the "actively harmful" case the brief calls out by name].
- [if] a proposed diff touches a workflow file, a ratchet baseline, or any
  gate/budget config then the guard rail flags it and the lane must not post it
  [else broken -- "never weaken a gate, baseline, ratchet, budget or threshold"].
- [if] a diff touches only ordinary source then the guard rail clears it [else a
  correct fix could never ship].
- [if] more fixers are already in_progress than the concurrency cap then no more
  capacity is reported [else the "2 concurrent" cap in #1017 is not enforced].
- [if] a fix comment is rendered then it contains root cause, a fenced diff, and
  a git apply command, and never claims to have pushed anything [else broken].
- [if] a diagnosis-only comment is rendered then it contains no diff block [else
  broken -- diagnosis-only must not smuggle an unverified diff].
"""

from __future__ import annotations

import pytest

from scripts import ci_fixer_core as cf


def _job(run_id=1, job_id=1, pr_number=42, runner_name="agentbox-2", job_name="pytest fast lane"):
    return cf.FailedJob(
        run_id=run_id,
        job_id=job_id,
        job_name=job_name,
        pr_number=pr_number,
        head_sha="a" * 40,
        runner_name=runner_name,
        workflow="ci.yml",
        html_url=f"https://github.com/{cf.REPO}/actions/runs/{run_id}",
        log_excerpt="",
    )


# ----- dedupe ledger -----------------------------------------------------------------


def test_dedupe_key_is_run_id_only_so_one_fixer_per_run():
    # Per #1017's guard rail: "one fixer per failed run, deduped by run id". Two
    # failing jobs in the SAME run must collapse to one dedupe key.
    a = _job(run_id=1, job_id=1)
    b = _job(run_id=1, job_id=2)
    c = _job(run_id=2, job_id=1)
    assert a.dedupe_key == b.dedupe_key
    assert a.dedupe_key != c.dedupe_key


def test_already_seen_false_for_fresh_ledger():
    ledger = {"entries": {}}
    assert cf.already_seen(ledger, _job()) is False


def test_record_outcome_then_already_seen_true():
    ledger = {"entries": {}}
    job = _job()
    outcome = cf.FixOutcome(job=job, disposition="diagnosis-only", reason="no repro")
    ledger = cf.record_outcome(ledger, outcome)
    assert cf.already_seen(ledger, job) is True
    # a different job_id on the SAME run is still deduped (one fixer per run)
    assert cf.already_seen(ledger, _job(job_id=99)) is True
    # a different run is a distinct failure, not deduped
    assert cf.already_seen(ledger, _job(run_id=2)) is False


def test_ledger_round_trips_through_disk(tmp_path):
    path = tmp_path / "ci-fixer" / "ledger.json"
    ledger = cf.record_outcome(
        {"entries": {}},
        cf.FixOutcome(job=_job(), disposition="known-unfixable", reason="orphaned-provenance-sha"),
    )
    cf.save_ledger(ledger, path)
    reloaded = cf.load_ledger(path)
    assert cf.already_seen(reloaded, _job()) is True


def test_load_ledger_missing_file_returns_empty():
    ledger = cf.load_ledger(cf.LEDGER_PATH.parent / "definitely-does-not-exist.json")
    assert ledger == {"entries": {}}


def test_load_ledger_corrupt_json_raises_precondition(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(cf.PreconditionError):
        cf.load_ledger(path)


# ----- concurrency cap -----------------------------------------------------------------


def test_concurrency_available_full_when_empty():
    ledger = {"entries": {}}
    assert cf.concurrency_available(ledger) == cf.MAX_CONCURRENT_FIXERS


def test_concurrency_available_shrinks_with_in_progress():
    ledger = {"entries": {}}
    ledger = cf.mark_in_progress(ledger, _job(run_id=1))
    assert cf.concurrency_available(ledger) == cf.MAX_CONCURRENT_FIXERS - 1
    ledger = cf.mark_in_progress(ledger, _job(run_id=2))
    assert cf.concurrency_available(ledger) == cf.MAX_CONCURRENT_FIXERS - 2


def test_concurrency_available_never_negative():
    ledger = {"entries": {}}
    for i in range(cf.MAX_CONCURRENT_FIXERS + 5):
        ledger = cf.mark_in_progress(ledger, _job(run_id=i))
    assert cf.concurrency_available(ledger) == 0


def test_in_progress_becomes_done_after_record_outcome_and_frees_capacity():
    ledger = {"entries": {}}
    job = _job()
    ledger = cf.mark_in_progress(ledger, job)
    assert cf.count_in_progress(ledger) == 1
    outcome = cf.FixOutcome(job=job, disposition="fix-proposed", reason="ok")
    ledger = cf.record_outcome(ledger, outcome, status="done")
    assert cf.count_in_progress(ledger) == 0


# ----- known-unfixable detection (issue #1029) ------------------------------------------


REAL_1029_PRECONDITION = (
    "[ERROR] precondition: commit b9a16ab8ac3af26051dcc9aa5ec4be26eb9eeddd is not "
    "present in this clone, so ancestry cannot be decided."
)
REAL_1029_SEED_SHA = "AssertionError: seed sha broken: 8924797"
REAL_1029_UNRESOLVABLE = (
    "AssertionError: controller-flx10 cites unresolvable sha 139c6821"
)


@pytest.mark.parametrize(
    "log_excerpt",
    [REAL_1029_PRECONDITION, REAL_1029_SEED_SHA, REAL_1029_UNRESOLVABLE],
)
def test_classify_known_unfixable_matches_real_1029_signatures(log_excerpt):
    assert cf.classify_known_unfixable(log_excerpt) == "orphaned-provenance-sha"


def test_classify_known_unfixable_none_for_ordinary_failure():
    log_excerpt = "AssertionError: assert 2 == 3\n  where 2 = add(1, 1)"
    assert cf.classify_known_unfixable(log_excerpt) is None


def test_known_unfixable_comment_declines_without_inventing_a_sha():
    job = _job()
    body = cf.render_known_unfixable_comment(job, "orphaned-provenance-sha")
    assert "#1029" in body
    assert "No diff proposed" in body
    # the whole point: never fabricate a replacement SHA in the comment
    assert "```diff" not in body


# ----- guard rails: never touch a gate/ratchet/budget/workflow file ----------------------


GOOD_DIFF = """diff --git a/apps/shared/foo.py b/apps/shared/foo.py
index e69de29..4b825dc 100644
--- a/apps/shared/foo.py
+++ b/apps/shared/foo.py
@@ -1 +1 @@
-x = 1
+x = 2
"""


@pytest.mark.parametrize(
    "bad_path",
    [
        ".github/workflows/ci.yml",
        "ops/quality/baseline.json",
        ".importlinter",
        "scripts/bench/kpi_ledger.json",
        "data/progress-tree.yaml",
        "apps/webui/frontend/new_size_ratchet.json",
        "apps/some_budget_config.py",
    ],
)
def test_diff_touches_guarded_paths_flags_gate_configs(bad_path):
    diff_text = f"""diff --git a/{bad_path} b/{bad_path}
index e69de29..4b825dc 100644
--- a/{bad_path}
+++ b/{bad_path}
@@ -1 +1 @@
-x = 1
+x = 2
"""
    touched = cf.diff_touches_guarded_paths(diff_text)
    assert bad_path in touched


def test_diff_touches_guarded_paths_clean_for_ordinary_source():
    assert cf.diff_touches_guarded_paths(GOOD_DIFF) == []


def test_diff_touches_guarded_paths_flags_only_the_guarded_file_in_mixed_diff():
    mixed = GOOD_DIFF + """diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
index e69de29..4b825dc 100644
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1 +1 @@
-a
+b
"""
    touched = cf.diff_touches_guarded_paths(mixed)
    assert touched == [".github/workflows/ci.yml"]


def test_diff_touches_guarded_paths_decodes_quoted_workflow_paths():
    diff_text = '''diff --git "a/.github/workflows/my gate.yml" "b/.github/workflows/my gate.yml"
index e69de29..4b825dc 100644
--- "a/.github/workflows/my gate.yml"
+++ "b/.github/workflows/my gate.yml"
@@ -1 +1 @@
-a
+b
'''
    assert cf.diff_touches_guarded_paths(diff_text) == [".github/workflows/my gate.yml"]


# ----- comment rendering -----------------------------------------------------------------


def test_fix_comment_contains_root_cause_diff_and_apply_command():
    job = _job()
    body = cf.render_fix_comment(job, "off-by-one in the retry loop", GOOD_DIFF)
    assert "off-by-one in the retry loop" in body
    assert "```diff" in body
    assert GOOD_DIFF.strip() in body
    assert "git apply" in body
    # never claims to have pushed
    assert "pushed" not in body.lower()
    assert "does not push" in body
    assert cf.outcome_marker(job) in body


def test_fix_comment_flags_suspect_runner():
    job = _job(runner_name="agentbox")
    body = cf.render_fix_comment(job, "cause", GOOD_DIFF)
    assert "pre-rewrite clone" in body


def test_fix_comment_no_suspect_note_for_clean_runner():
    job = _job(runner_name="agentbox-2")
    body = cf.render_fix_comment(job, "cause", GOOD_DIFF)
    assert "pre-rewrite clone" not in body


def test_diagnosis_only_comment_has_no_diff_block():
    job = _job()
    body = cf.render_diagnosis_comment(job, "could not isolate the flake within the cap")
    assert "```diff" not in body
    assert "could not isolate the flake within the cap" in body
    assert "No diff proposed" in body


# ----- KPI events -------------------------------------------------------------------------


def test_kpi_events_round_trip_through_disk(tmp_path):
    path = tmp_path / "kpi-events.jsonl"
    job1, job2 = _job(run_id=1), _job(run_id=2)
    cf.append_kpi_event(
        cf.FixOutcome(job=job1, disposition="fix-proposed", reason="ok"), path
    )
    cf.append_kpi_event(
        cf.FixOutcome(job=job2, disposition="known-unfixable", reason="orphaned-provenance-sha"),
        path,
    )
    events = cf.load_kpi_events(path)
    assert len(events) == 2
    assert events[0]["disposition"] == "fix-proposed"
    assert events[1]["disposition"] == "known-unfixable"
    # every event carries runner_name, per #1017's live-context requirement
    assert all("runner_name" in e for e in events)


def test_append_kpi_event_is_idempotent_for_a_retried_run(tmp_path):
    path = tmp_path / "kpi-events.jsonl"
    outcome = cf.FixOutcome(job=_job(run_id=3), disposition="diagnosis-only", reason="retry")
    cf.append_kpi_event(outcome, path)
    cf.append_kpi_event(outcome, path)
    assert len(cf.load_kpi_events(path)) == 1


def test_load_kpi_events_missing_file_returns_empty_list(tmp_path):
    assert cf.load_kpi_events(tmp_path / "nope.jsonl") == []


def test_load_kpi_events_corrupt_line_raises_precondition(tmp_path):
    path = tmp_path / "kpi-events.jsonl"
    path.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(cf.PreconditionError):
        cf.load_kpi_events(path)


def test_build_kpi_report_counts_each_disposition():
    events = [
        {"disposition": "fix-proposed"},
        {"disposition": "fix-proposed"},
        {"disposition": "diagnosis-only"},
        {"disposition": "known-unfixable"},
    ]
    report = cf.build_kpi_report(events)
    assert report.total_events == 4
    assert report.fixes_proposed == 2
    assert report.diagnosis_only == 1
    assert report.known_unfixable == 1
    assert report.runner_environment_share == pytest.approx(0.25)


def test_build_kpi_report_empty_events_share_is_none_not_zero():
    report = cf.build_kpi_report([])
    assert report.total_events == 0
    assert report.runner_environment_share is None
