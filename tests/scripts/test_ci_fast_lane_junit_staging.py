"""The pytest fast lane writes, stages and hands off per-shard JUnit reports for test health.

The reports feed Trunk Flaky Tests from the isolated `ci-insights` job
(tests/scripts/test_ci_trunk_flaky_shadow.py pins the upload itself). This module
pins the half that lives in the `test` job: the report is written where no earlier
run can reach, staged only when pytest really ran, and handed across the job
boundary without a stale or foreign report standing in for this run's result.
Until Wed 30 Sep 2026 this module also pinned a second, per-shard uploader that
ADR-NEW-trunk-is-the-only-merge-queue removed; the staging tests are unchanged.

MEASURE ONLY: the shard's verdict is still `exit "$rc"` from pytest itself.

Regression lines:
  - if the pytest step drops --junitxml then no report is written, the
    upload has nothing to send, and the lane is silently unmeasured
  - if pytest's per-shard filename and the upload's junit glob stop agreeing
    then the upload finds nothing while both halves still look correctly configured
  - if either filename stops varying per shard then five shards race on one
    path and whichever finishes last is the only one recorded
  - if the staging step stops reading the pytest step's own outcome then a
    pytest that never ran can stage a report an earlier run left behind
  - if the staging directory can be reused then a dead shard uploads a previous run
  - if staging becomes fatal then broken instrumentation fails a green suite
  - if a staging failure does not suppress the artifact upload then a failed
    cleanup publishes stale data
  - if the prune globs widely then it deletes another shard's result before its upload
  - if the report is written outside RUNNER_TEMP then a stale one can be staged
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

SHARD_JOB = "test"
# The upload lives in its OWN job, with no checkout step, so the uploader's token
# is never in scope in a job that runs repository code (Sol P1 on #3256, round 2).
INSIGHTS_JOB = "ci-insights"
STAGE_STEP_NAME = "Stage this shard's JUnit report"
STAGE_STEP_ID = "stage-insights"
PYTEST_STEP_ID = "pytest-shard"
DOWNLOAD_STEP_ID = "download"
UPLOAD_STEP_ID = "trunk-flaky-tests"
GUARD_NAME = "The Trunk uploads must have run"

# A matrix expression has to appear in the filenames, or the five shards collide.
SHARD_EXPR = "matrix.shard"

# The ci-insights job has no strategy.matrix of its own (ADR-NEW-ci-insights-cost-gate):
# its per-shard steps carry an explicit `-N` suffix instead.
INSIGHTS_SHARDS = (1, 2, 3, 4, 5)


def _insights_id(base: str, shard: int) -> str:
    return f"{base}-{shard}"


def _load_ci() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _shard_job() -> dict:
    return _load_ci()["jobs"][SHARD_JOB]


def _job(name: str) -> dict:
    jobs = _load_ci()["jobs"]
    assert name in jobs, (
        f"ci.yml has no job named {name!r}. The CI Insights wiring is "
        f"addressed by job name; renaming one without the other turns every "
        f"check below into one that cannot fire. Jobs present: {sorted(jobs)}"
    )
    return jobs[name]


def _steps(job: str = SHARD_JOB) -> list[dict]:
    return _job(job)["steps"]


def _step_by_id(step_id: str, job: str = INSIGHTS_JOB) -> dict:
    for step in _steps(job):
        if step.get("id") == step_id:
            return step
    raise AssertionError(
        f"ci.yml job '{job}' has no step with id '{step_id}'. "
        "The CI Insights wiring is addressed by id, so renaming one without "
        "the other turns the check into one that cannot fire."
    )


def _pytest_run_block() -> str:
    return _step_by_id(PYTEST_STEP_ID, SHARD_JOB)["run"]


def test_pytest_shard_writes_a_junit_report() -> None:
    """if the pytest step drops --junitxml then nothing is ever measured."""
    run = _pytest_run_block()
    assert "--junitxml=" in run, (
        "The pytest fast lane no longer writes a JUnit report, so the CI "
        "Insights upload has nothing to send and this lane records no test "
        "health at all -- while every step still reads as green."
    )


def _junitxml_path() -> str:
    match = re.search(r'--junitxml="([^"]+)"', _pytest_run_block())
    assert match, (
        "--junitxml is present but not in the quoted form this test reads. "
        'Keep it as --junitxml="<path>" so the report path stays greppable '
        "and can be compared against the upload's report_path."
    )
    return match.group(1)


def _stage_step() -> dict:
    for step in _steps(SHARD_JOB):
        if STAGE_STEP_NAME in step.get("name", ""):
            return step
    raise AssertionError(
        f"The {STAGE_STEP_NAME!r} step is gone from job {SHARD_JOB!r}. It is "
        "the only thing carrying the JUnit report across the job boundary, "
        "so without it the isolated upload job downloads an empty artifact "
        "and records nothing, while every step still reads green."
    )


def test_junit_report_path_is_per_shard() -> None:
    """if the path stops varying then five shards race on one file."""
    written = _junitxml_path()
    assert SHARD_EXPR in written, (
        f"The JUnit report path {written!r} does not vary per shard, so all "
        "five shards write the same file on their own runners and only one "
        "shard's results can ever be attributed correctly."
    )


def test_a_stale_report_cannot_be_staged_as_this_runs_result() -> None:
    """if the staging dir is reused then a dead shard uploads a previous run."""
    stage = _stage_step()
    stage_dir = stage.get("env", {}).get("STAGE_DIR", "")
    assert stage_dir, (
        "The staging step no longer names a STAGE_DIR. The previous revision "
        "cleared a FIXED directory with `rm -rf`, which this script's lack of "
        "errexit made unsafe: a failed rm fell through to a `mkdir -p` that "
        "reused the surviving directory and the step still reported success. "
        "The directory must instead be named so that reuse is impossible."
    )
    # The name has to distinguish a RE-RUN of the same run, not just two runs:
    # run_id alone repeats across attempts, which is exactly the case that
    # produced two reds on one head while re-running this very pull request.
    for token in ("github.run_id", "github.run_attempt", SHARD_EXPR):
        assert token in stage_dir, (
            f"STAGE_DIR is {stage_dir!r}, which does not vary by {token}. Two "
            "executions that share a directory name on this persistent "
            "workspace can hand one another's JUnit report to the upload job, "
            "and an absence rendered as a measurement is the one outcome this "
            "lane must never produce."
        )
    assert stage["run"].count(stage_dir) == 0, (
        "The run block interpolates the directory expression directly rather "
        "than reading $STAGE_DIR, so the name is repeated in several places "
        "and can drift between them."
    )


def test_staging_failure_cannot_redden_the_lane() -> None:
    """if staging is fatal then broken instrumentation fails a green suite."""
    assert _stage_step().get("continue-on-error") is True, (
        "The staging step is fatal again. A transient mkdir, copy, permission "
        "or disk-space failure would redden the whole test job even when "
        "pytest passed, which contradicts the measure-only contract that the "
        "artifact upload below it already honors. The isolated job's "
        "missing-report guard is what should expose an instrumentation failure."
    )


def test_staging_refuses_a_report_the_pytest_step_did_not_write() -> None:
    """if staging ignores the outcome then a skipped pytest uploads an old run."""
    run = _stage_step()["run"]
    # The rm -f that clears a previous run's report lives INSIDE the pytest
    # step. When checkout or any earlier step fails, pytest is skipped, that rm
    # never runs, and this always() step would find the earlier run's report
    # still in the persistent workspace. A skipped pytest has no result, and no
    # result must travel as an absence rather than as the last report lying
    # around (Sol P1 on #3447, round 2).
    assert "PYTEST_OUTCOME" in run, (
        "The staging step no longer consults the pytest step's outcome before "
        "copying, so a run in which pytest never executed can still stage a "
        "report and have it recorded as this run's measurement."
    )
    cp_at = run.find("cp ")
    # Anchored to the WHOLE case arm, not searched as a substring. `success|
    # failure` is a substring of `success|failure|skipped|*)`, so a `find`
    # keeps passing for a gate widened to accept everything -- this guard was
    # written that way first and a mutation walked straight through it.
    arm = re.search(r"^[ \t]*success\|failure\)[ \t]*$", run, re.MULTILINE)
    assert arm is not None, (
        "The staging step does not gate the copy on pytest having actually "
        "run, and ONLY on that. The accepted outcomes must be exactly "
        "success and failure. Any other outcome -- skipped, cancelled, or an "
        "empty string from a step that never started -- means there is no "
        "result to stage, so widening the arm to admit one reintroduces the "
        "defect this gate exists to prevent."
    )
    gate_at = arm.start()
    assert cp_at != -1 and gate_at < cp_at, (
        f"The outcome gate is at {gate_at} and the copy at {cp_at}: the gate "
        "must come FIRST or it cannot prevent the copy it exists to prevent."
    )


def test_a_staging_failure_suppresses_the_upload() -> None:
    """if the upload ignores staging then a failed cleanup publishes stale data."""
    upload = None
    for step in _steps(SHARD_JOB):
        if str(step.get("uses", "")).startswith("actions/upload-artifact@") and (
            "ci-insights" in str(step.get("with", {}).get("name", ""))
        ):
            upload = step
            break
    assert upload is not None, (
        "The shard job no longer uploads a ci-insights artifact, so the "
        "isolated job has nothing to read and the lane records nothing."
    )
    condition = str(upload.get("if", ""))
    assert f"steps.{STAGE_STEP_ID}.outcome" in condition, (
        f"The artifact upload runs on {condition!r}, which does not consult "
        "the staging step. Staging is continue-on-error, so when it fails the "
        "job carries on and an unconditional upload publishes whatever is in "
        "the directory -- which, on a failed cleanup, is an earlier run's "
        "report presented as this one's measurement."
    )
    # `conclusion` is rewritten to success by continue-on-error, so a check
    # reading it would pass for a staging step that actually failed. This is
    # the same class as a wrapper exit code reporting success for an inner
    # command that failed.
    assert f"steps.{STAGE_STEP_ID}.conclusion" not in condition, (
        f"The upload condition reads `conclusion` in {condition!r}. "
        "continue-on-error rewrites conclusion to success, so this condition "
        "is true even when staging failed: it looks like a guard and cannot "
        "ever refuse. Read `outcome`, which is the result before the rewrite."
    )
    assert "always()" in condition, (
        "The upload lost always(), so the runs most worth recording -- the "
        "red ones -- stop being uploaded at all."
    )


def test_the_prune_cannot_delete_another_shards_staging_directory() -> None:
    """if the prune globs widely then it deletes a result before its upload."""
    run = _stage_step()["run"]
    prune = next((ln for ln in run.splitlines() if "-maxdepth 1" in ln), "")
    assert prune or "find" in run, "The prune disappeared entirely."
    # Reconstruct the whole find invocation: it is wrapped across lines.
    start = run.find("find .")
    assert start != -1, "No find-based prune in the staging step."
    invocation = run[start : run.find("\n", run.find("-exec", start))]
    assert SHARD_EXPR in invocation, (
        f"The prune {invocation!r} is not scoped to this shard, so it matches "
        "directories belonging to OTHER shards in a shared workspace. "
        "Deleting a directory another job has written but not yet uploaded "
        "turns a real result into a missing artifact -- the same defect this "
        "change exists to prevent, pointed the other way."
    )
    assert "-mmin" in invocation, (
        f"The prune {invocation!r} carries no obsolescence proof, so it can "
        "match a directory that is still in flight. A shard's wall budget "
        "bounds how old an in-flight directory can be; require an age past it."
    )


def test_the_junit_report_is_written_where_a_previous_run_cannot_reach() -> None:
    """if the report lands in the workspace then a stale one can be staged."""
    # This is the CLASS fix for four rounds of P1s on #3447, each of which was
    # a different route by which a previous run's report at a fixed workspace
    # path got treated as current: staging reused a directory, staging ran
    # when pytest had not, the cleanup could fail silently, and then the
    # cleanup's own failure produced an outcome the staging gate accepted.
    # The runner empties RUNNER_TEMP at the start and end of every job, so a
    # report written there cannot have been left by an earlier run and no
    # cleanup of ours has to succeed for that to hold.
    written = _junitxml_path()
    assert "RUNNER_TEMP" in written, (
        f"pytest writes its JUnit report to {written!r}, which is not under "
        "RUNNER_TEMP. A workspace path persists between runs on a self-hosted "
        "runner, so an earlier run's report can be staged and uploaded as "
        "this run's measurement. Every guard against that is a guard that has "
        "to work; RUNNER_TEMP removes the situation instead."
    )
    # The staging step must read from the SAME place, or the report it copies
    # is once again whatever is sitting in the workspace.
    assert written in _stage_step()["run"], (
        f"The staging step does not read {written!r}. Writing the report "
        "somewhere safe and then copying from somewhere else reinstates the "
        "defect while looking like the fix for it."
    )
    # Anchored to the COPY ITSELF, not to the path appearing anywhere in the
    # step. The first version of this assertion checked that `written` was
    # present somewhere in the run block, which stayed true when only the `cp`
    # was pointed back at the workspace -- the `if [ -f ... ]` test above it
    # still mentioned the safe path. A mutation walked through it.
    cp_lines = [ln for ln in _stage_step()["run"].splitlines() if "cp " in ln]
    assert cp_lines, "The staging step no longer copies the report anywhere."
    for line in cp_lines:
        assert "RUNNER_TEMP" in line, (
            f"The staging step copies from {line.strip()!r}, which is not the "
            "RUNNER_TEMP path pytest writes. Checking that the safe path is "
            "mentioned SOMEWHERE in the step is not enough: the guard and the "
            "copy can name different files while the step still reads as "
            "correct, which is exactly how this assertion failed its own "
            "mutation test."
        )


def _guard_step() -> dict:
    for step in _steps(INSIGHTS_JOB):
        if step.get("name") == GUARD_NAME:
            return step
    raise AssertionError(
        f"The {GUARD_NAME!r} step is gone from job {INSIGHTS_JOB!r}. Without it a "
        "failed transfer, a suppressed staging step and a shard that genuinely wrote "
        "no report all read the same, and an upload given zero reports reads as a success."
    )


def test_the_junit_filename_is_what_the_upload_glob_sends() -> None:
    """if the two disagree then the upload finds nothing, and looks fine."""
    written_name_template = PurePosixPath(_junitxml_path()).name
    assert SHARD_EXPR in written_name_template, (
        f"pytest's JUnit filename {written_name_template!r} no longer varies "
        f"by {SHARD_EXPR}; re-derive the per-shard comparison below."
    )
    for shard in INSIGHTS_SHARDS:
        glob = _step_by_id(_insights_id(UPLOAD_STEP_ID, shard))["with"]["junit-paths"]
        pattern = re.compile("^" + re.escape(glob).replace(r"\*", "[^/]*") + "$")
        path = _step_by_id(_insights_id(DOWNLOAD_STEP_ID, shard))["with"]["path"]
        name = written_name_template.replace("${{ matrix.shard }}", str(shard))
        assert pattern.match(f"{path}/{name}"), (
            f"pytest writes {name!r} for shard {shard} and it is downloaded into "
            f"{path!r}, but the upload sends {glob!r}, which does not match. Both "
            "halves look correctly configured in isolation, so this fails as an "
            "empty dashboard rather than as a red check."
        )
        assert not pattern.match(f"{path}/outcome.txt"), "negative control"
    assert _junitxml_path() in _stage_step()["run"], (
        "The staging step does not copy the report pytest writes, so nothing the "
        "upload job downloads will contain it."
    )


def test_staging_reads_the_pytest_steps_own_outcome() -> None:
    """if the pytest id and the staging gate stop agreeing then the gate cannot fire."""
    stage = _stage_step()
    # The outcome reaches the script through the step env rather than a direct
    # interpolation, so both halves are searched: naming only `run` would turn
    # this into a check that cannot fire the moment the wiring moves.
    stage_text = str(stage.get("env", {})) + stage["run"]
    assert f"steps.{PYTEST_STEP_ID}.outcome" in stage_text, (
        "The staging step no longer reads the pytest step's own outcome, so its "
        "success|failure gate reads an empty string and a pytest that never ran can "
        "no longer be told apart from one that did."
    )
