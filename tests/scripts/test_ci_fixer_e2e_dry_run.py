"""E2E dry run against a deliberately failing branch (issue #1017, requirement 6).

Builds a REAL git worktree with a REAL deliberately-failing test, runs pytest for
real (no synthetic log text), captures the actual failure output, and feeds it
through the real ci_fixer.poll() pipeline in --dry-run mode: detect -> classify
-> (would-be guardrail check) -> render.

Exercises the wiring end to end EXCEPT the codex worker spawn: a real codex call
is subscription-billed, non-deterministic, and up to 20 minutes, wrong for a
suite gating every PR's fast lane. `--dry-run` is a first-class CLI mode
(mirroring trunk_job_verdict.py's `--runs-json`), not a mock -- the same code
path `--poll` uses up to the point a worker would be spawned.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts import ci_fixer
from scripts import ci_fixer_core as cf

FAILING_TEST = '''
def test_deliberately_wrong():
    assert 1 + 1 == 3, "this is the deliberately failing fixture for the ci-fixer e2e test"
'''


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def test_e2e_dry_run_against_a_real_deliberately_failing_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q"], cwd=repo)
    _run(["git", "config", "user.email", "ci-fixer-e2e@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "ci-fixer-e2e"], cwd=repo)
    (repo / "test_deliberately_failing.py").write_text(FAILING_TEST, encoding="utf-8")
    _run(["git", "add", "-A"], cwd=repo)
    _run(["git", "commit", "-q", "-m", "deliberately failing fixture"], cwd=repo)

    # Run the REAL failing test with the REAL test runner and capture REAL output --
    # this is the log excerpt a self-hosted runner's `gh run view --log-failed`
    # would hand the lane in --poll mode.
    result = _run(
        [sys.executable, "-m", "pytest", "-q", "test_deliberately_failing.py"],
        cwd=repo,
    )
    assert result.returncode != 0, "fixture must actually fail, or this test proves nothing"
    log_excerpt = result.stdout + result.stderr
    assert "test_deliberately_wrong" in log_excerpt

    # This is an ORDINARY code failure, not #1029's signature -- confirm the
    # classifier agrees before trusting the rest of the pipeline.
    assert cf.classify_known_unfixable(log_excerpt) is None

    job_payload = [
        {
            "run_id": 999001,
            "job_id": 1,
            "job_name": "pytest fast lane",
            "pr_number": 4242,
            "head_sha": "c" * 40,
            "runner_name": "agentbox-2",
            "workflow": "ci.yml",
            "html_url": f"https://github.com/{cf.REPO}/actions/runs/999001",
            "log_excerpt": log_excerpt,
            "created_at": "2026-09-03T00:00:00Z",
        }
    ]
    jobs_json = tmp_path / "jobs.json"
    jobs_json.write_text(json.dumps(job_payload), encoding="utf-8")

    original_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        outcomes = ci_fixer.poll(dry_run=True, jobs_json=jobs_json)
    finally:
        os.chdir(original_cwd)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.disposition == "diagnosis-only"
    assert outcome.job.run_id == 999001
    # dry-run never spawns a worker or posts anything
    assert "dry-run" in outcome.reason
    assert "```diff" not in outcome.comment_body

    # A live Actions log frames the real command in a `Run` group. The lane must
    # re-run that complete command, never infer a smaller test target from the
    # summary text after this real failure.
    action_log = (
        "job\tUNKNOWN\t2026-09-03T00:00:00.0000000Z "
        "##[group]Run python3 -m pytest -q test_deliberately_failing.py\n"
        "job\tUNKNOWN\t2026-09-03T00:00:00.0000001Z "
        "python3 -m pytest -q test_deliberately_failing.py\n"
        "job\tUNKNOWN\t2026-09-03T00:00:00.0000002Z shell: /usr/bin/bash -e {0}\n"
    )
    recheck = ci_fixer.extract_failed_step_command(action_log)
    assert recheck == ["python3", "-m", "pytest", "-q", "test_deliberately_failing.py"]

    # Ledger + KPI event were really written to disk.
    job = cf.FailedJob(**job_payload[0])
    ledger = cf.load_ledger(tmp_path / ".planning" / "ci-fixer" / "ledger.json")
    assert cf.already_seen(ledger, job)
    events = cf.load_kpi_events(tmp_path / ".planning" / "ci-fixer" / "kpi-events.jsonl")
    assert len(events) == 1
    assert events[0]["run_id"] == 999001
