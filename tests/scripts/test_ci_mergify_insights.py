"""The pytest fast lane records per-test health in Mergify CI Insights.

Round 1 is MEASURE ONLY (DevOps ruling, Wed 16 Sep 2026): the shard's verdict
is still `exit "$rc"` from pytest itself, and the CI Insights steps only
record. Quarantine, which can subtract a known-flaky failure from an exit
code, is deliberately NOT armed until there is a baseline to arm it against.

The value this lane buys is one defect this repo has a measured instance of:
a SILENT FAILURE, where the runner exits non-zero while the JUnit report
carries no failures (a collection error, an OOM, a crashed worker), so a gate
certifies something it never measured. scripts/affected_tests.py returned 0
modules with rc=0 on a bad path and its canary read green for 25 of 28 runs
without executing a single test. `test_step_outcome` is what detects that
shape structurally instead of by someone noticing.

Regression lines:
  - if the pytest step drops --junitxml then no report is written, the
    upload has nothing to send, and the lane is silently unmeasured
  - if --junitxml and report_path stop naming the same file then the upload
    finds nothing while both halves still look correctly configured
  - if either filename stops varying per shard then five shards race on one
    path and whichever finishes last is the only one recorded
  - if job_name is dropped then the action defaults to the GitHub job name,
    which is identical across all five shards, and every shard's results are
    filed under one name so the per-test history is worthless
  - if job_name stops varying per shard it fails the same way, silently
  - if test_step_outcome is dropped then MERGIFY_TEST_EXIT_CODE is never set
    and silent-failure detection -- the reason this lane was wired first --
    never fires, while uploads keep succeeding so nothing looks wrong
  - if the pytest step's id and the outcome expression stop agreeing then
    test_step_outcome resolves to an empty string, which the action treats
    the same as omitting it, so detection is off with no error anywhere
  - if the CI Insights step loses continue-on-error then a Mergify outage,
    rate limit or expired token paints every pull request red on a lane that
    is only supposed to be measuring
  - if the upload-verdict guard is dropped then a 'rejected' upload records
    NO test data while the step still reads as having run, and the flake
    baseline is built from missing runs
  - if the guard stops distinguishing 'rejected' from 'failed' from empty
    then a permanent scope problem reads like a transient blip
  - if the action stops being pinned to a full commit SHA then a third party
    can change what runs inside this repo's CI without review
  - if MERGIFY_TOKEN reaches the job env, or any step other than the probe and
    the upload action, then pytest and every subprocess the suite spawns can
    read it (Sol P1 on #3256)
  - if the probe step runs after checkout then repository code runs with the
    secret in scope
  - if the steps stop gating on the token then every shard of every pull
    request annotates before the secret exists, which is pure noise

Round 2 (ADR-NEW-ci-insights-cost-gate, Sun 20 Sep 2026): the upload was
REJECTED with HTTP 422 on every shard since round 1 shipped -- CI Insights
was never enabled on the Mergify side -- so the job is now gated behind
vars.CI_INSIGHTS_ENABLED (unset by default) and the 5-way strategy.matrix was
collapsed into ONE job that repeats each shard's steps with an explicit `-N`
suffix (`download-1`..`download-5`, `mergify-insights-1`..`mergify-insights-5`,
etc.) instead of relying on `matrix.shard` to vary them at runtime. The
per-shard assertions below loop over INSIGHTS_SHARDS instead of reading a
single matrix-templated step, but check exactly the same properties.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

SHARD_JOB = "test"
# The CI Insights upload lives in its OWN job, with no checkout step, so that
# secrets.MERGIFY_TOKEN is never in scope in a job that runs repository code
# (Sol P1 on #3256, round 2). Step-level scoping was the first answer and it
# is not sufficient on a self-hosted runner: the job's processes outlive the
# step, so a test that forks a background process can read the upload action's
# INPUT_TOKEN when it appears. The job boundary is the control.
INSIGHTS_JOB = "ci-insights"
STAGE_STEP_NAME = "Stage this shard's JUnit report"
STAGE_STEP_ID = "stage-insights"
OUTCOME_STEP_ID = "shard-outcome"
PYTEST_STEP_ID = "pytest-shard"
UPLOAD_STEP_ID = "mergify-insights"
PROBE_STEP_ID = "mergify-token"
DOWNLOAD_STEP_ID = "download"
ACTION_REPO = "mergifyio/gha-mergify-ci"

# A matrix expression has to appear in both filenames and in job_name, or the
# five shards collide. This is the substring every one of them must carry.
SHARD_EXPR = "matrix.shard"

# Round 2 collapsed the ci-insights job's own 5-way strategy.matrix into one
# job whose steps carry an explicit `-N` suffix instead. These five numbers
# are the only place that shard identity now lives in this test module.
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
        "Keep it as --junitxml=\"<path>\" so the report path stays greppable "
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


def test_junit_report_path_matches_the_upload_report_path() -> None:
    """if the two disagree then the upload finds nothing, and looks fine."""
    written = _junitxml_path()
    written_name_template = PurePosixPath(written).name
    assert SHARD_EXPR in written_name_template, (
        f"pytest's JUnit filename {written_name_template!r} no longer varies "
        f"by {SHARD_EXPR}; re-derive the per-shard comparison below."
    )
    # The insights job has no matrix of its own since round 2, so each of its
    # five upload steps must read the LITERAL substitution of the `test`
    # job's per-shard filename template, not the unresolved expression.
    for shard in INSIGHTS_SHARDS:
        uploaded = _step_by_id(_insights_id(UPLOAD_STEP_ID, shard))["with"]["report_path"]
        expected_name = written_name_template.replace("${{ matrix.shard }}", str(shard))
        # The upload reads the DOWNLOADED copy, so the paths differ by the
        # download directory. The invariant that matters is that the same
        # per-shard filename is written, staged and then read.
        assert PurePosixPath(uploaded).name == expected_name, (
            f"pytest writes {written!r} (shard {shard} resolves to "
            f"{expected_name!r}) but shard {shard}'s CI Insights step "
            f"uploads {uploaded!r}, and the filenames do not match. Both "
            "halves look correctly configured in isolation, so this fails "
            "as an empty dashboard rather than as a red check."
        )
    assert written in _stage_step()["run"], (
        f"The staging step does not copy {written!r}, so nothing the upload "
        "job downloads will contain the report it is configured to read."
    )


def test_junit_report_path_is_per_shard() -> None:
    """if the path stops varying then five shards race on one file."""
    written = _junitxml_path()
    assert SHARD_EXPR in written, (
        f"The JUnit report path {written!r} does not vary per shard, so all "
        "five shards write the same file on their own runners and only one "
        "shard's results can ever be attributed correctly."
    )


def test_upload_step_names_the_job_per_shard() -> None:
    """if job_name is dropped then all five shards file under one name."""
    seen_names = set()
    for shard in INSIGHTS_SHARDS:
        with_block = _step_by_id(_insights_id(UPLOAD_STEP_ID, shard))["with"]
        assert "job_name" in with_block, (
            f"job_name is missing on shard {shard}. The action defaults the "
            "test-job name to the GitHub job name, which -- since round 2 "
            "collapsed all five shards into ONE job -- is now identical "
            "across all five, so every shard's results would be filed under "
            "one name and the per-test history this lane exists to produce "
            "is worthless. The action's own input documentation calls this "
            "out for matrix jobs, and it applies just as much to five "
            "unrolled step-groups in a single job."
        )
        assert str(shard) in with_block["job_name"], (
            f"job_name {with_block['job_name']!r} does not name shard "
            f"{shard}, which fails exactly the same way as omitting it, "
            "and just as silently."
        )
        seen_names.add(with_block["job_name"])
    assert len(seen_names) == len(INSIGHTS_SHARDS), (
        f"job_name collides across shards: {seen_names}. Every shard must "
        "file under a distinct name or their results overwrite one another."
    )


def test_upload_step_passes_the_test_runner_outcome() -> None:
    """if test_step_outcome is dropped then silent failures stop being seen."""
    for shard in INSIGHTS_SHARDS:
        outcome_id = _insights_id(OUTCOME_STEP_ID, shard)
        with_block = _step_by_id(_insights_id(UPLOAD_STEP_ID, shard))["with"]
        outcome = with_block.get("test_step_outcome", "")
        assert outcome, (
            f"test_step_outcome is missing on shard {shard}, so "
            "MERGIFY_TEST_EXIT_CODE is never set and the CLI cannot flag a "
            "silent failure: a runner that exited non-zero while its JUnit "
            "report carries no failures. That is the single defect this "
            "lane was wired first to catch, and without this input uploads "
            "keep succeeding so nothing ever looks wrong."
        )
        assert f"steps.{outcome_id}.outputs.outcome" in outcome, (
            f"test_step_outcome is {outcome!r} on shard {shard}, which does "
            f"not read that shard's own recovered pytest outcome "
            f"(steps.{outcome_id}.outputs.outcome). An expression naming a "
            "step that does not exist resolves to an empty string, and the "
            "action treats empty the same as omitted -- so detection would "
            "be off with no error raised anywhere. It would also be worse "
            "than the old matrix shape if this named a DIFFERENT shard's "
            "step: uploads would keep succeeding while flagging the wrong "
            "shard's silent failures."
        )
        recovered = _step_by_id(outcome_id)["run"]
        assert "outcome.txt" in recovered, recovered

    # The outcome crosses a job boundary, so the chain has two links and both
    # have to hold: the test job writes the pytest step's own outcome into the
    # artifact, and the insights job reads it back out. This half of the
    # chain is per-shard only inside the `test` job (still a real matrix),
    # so it is checked once rather than once per insights-job shard.
    stage = _stage_step()
    # The outcome reaches the script through the step env rather than a direct
    # interpolation, so both halves are searched: naming only `run` would turn
    # this into a check that cannot fire the moment the wiring moves.
    stage_text = str(stage.get("env", {})) + stage["run"]
    assert f"steps.{PYTEST_STEP_ID}.outcome" in stage_text, (
        "The staging step no longer records the pytest step's own outcome, so "
        "the value the upload reads back is whatever happens to be in the "
        "file -- silent-failure detection would be off with nothing raised."
    )


def test_upload_step_cannot_redden_the_lane() -> None:
    """if it loses continue-on-error then a Mergify outage reddens PRs."""
    for shard in INSIGHTS_SHARDS:
        step = _step_by_id(_insights_id(UPLOAD_STEP_ID, shard))
        assert step.get("continue-on-error") is True, (
            f"The CI Insights step for shard {shard} must stay "
            "continue-on-error. Round 1 is measure-only: pytest's own exit "
            "code is the shard's verdict. Without this, a Mergify outage, a "
            "rate limit or an expired token paints every pull request red "
            "on a lane that is only recording."
        )


def test_upload_step_is_pinned_to_a_sha() -> None:
    """if the action is pinned to a tag then a third party can change CI."""
    for shard in INSIGHTS_SHARDS:
        uses = _step_by_id(_insights_id(UPLOAD_STEP_ID, shard))["uses"]
        assert uses.startswith(f"{ACTION_REPO}@"), uses
        ref = uses.split("@", 1)[1]
        assert re.fullmatch(r"[0-9a-f]{40}", ref), (
            f"The CI Insights action for shard {shard} is pinned to {ref!r}, "
            "not a full commit SHA. A moving tag lets a third party change "
            "what executes inside this repo's CI without any review here."
        )


def _guard_step() -> dict:
    for step in _steps(INSIGHTS_JOB):
        if "must have recorded something" in step.get("name", ""):
            return step
    raise AssertionError(
        "The CI Insights upload-verdict guard is gone. Without it a "
        "'rejected' upload records NO test data while the step still reads "
        "as having run, so the flake baseline is quietly built from missing "
        "runs -- a green step certifying an empty measurement, which is the "
        "defect .claude/rules/verification.md is written against."
    )


def test_guard_separates_rejected_from_failed_from_unknown() -> None:
    """if the guard collapses these then a scope bug reads like a blip."""
    run = _guard_step()["run"]
    for verdict in ("rejected", "failed"):
        assert f"{verdict})" in run, (
            f"The guard no longer handles a '{verdict}' upload outcome. "
            "'rejected' is permanent and means nothing was recorded (usually "
            "a token without CI Insights scope); 'failed' is transient. "
            "Collapsing them makes a persistent misconfiguration read like a "
            "blip nobody needs to act on."
        )
    assert "UNKNOWN" in run, (
        "The guard no longer reports an empty upload outcome as UNKNOWN. An "
        "outcome that could not be read is not the same as a successful "
        "upload, and must never render as one."
    )
    assert "::error" in run, (
        "The guard no longer emits an ::error annotation. A rejected upload "
        "has to be visible on the pull request; discovering it weeks later "
        "when the dashboard answers a question from no data is the failure "
        "this guard exists to prevent."
    )


def test_the_secret_never_shares_a_job_with_repository_code() -> None:
    """if the upload job gains a checkout then a test can read the token."""
    jobs = _load_ci()["jobs"]
    holders = sorted(
        name for name, job in jobs.items() if "secrets.MERGIFY_TOKEN" in json.dumps(job)
    )
    assert holders == [INSIGHTS_JOB], (
        f"secrets.MERGIFY_TOKEN is reachable from jobs {holders}, not only "
        f"from {INSIGHTS_JOB!r}. Any job that also runs repository code can "
        "leak it: on a self-hosted runner a test may fork a background "
        "process that outlives its step and read the secret out of a later "
        "step's environment, so scoping it to a step is not a boundary."
    )
    checkouts = [
        step.get("name")
        for step in _steps(INSIGHTS_JOB)
        if str(step.get("uses", "")).startswith("actions/checkout")
    ]
    assert checkouts == [], (
        f"Job {INSIGHTS_JOB!r} now checks out the repository ({checkouts}). "
        "That reintroduces exactly what this split removed: repository code "
        "running in the job that holds the credential."
    )


def test_the_probe_exports_only_a_boolean() -> None:
    """if the probe exports the value then every later step can read it."""
    run = _step_by_id(PROBE_STEP_ID)["run"]
    assert "present=true" in run and "present=false" in run, run
    assert "$MERGIFY_TOKEN" not in run.replace("${MERGIFY_TOKEN:-}", ""), run


def test_ci_insights_steps_are_skipped_without_a_token() -> None:
    """if they stop gating then every PR annotates before the secret exists."""
    steps = [_step_by_id(_insights_id(UPLOAD_STEP_ID, shard)) for shard in INSIGHTS_SHARDS]
    steps.append(_guard_step())
    for step in steps:
        condition = step.get("if", "")
        assert "steps.mergify-token.outputs.present == 'true'" in condition, (
            f"Step {step.get('name')!r} no longer skips when the token is "
            "unset. On forks and before the secret is provisioned this would "
            "annotate every shard of every pull request, which is noise on a "
            "lane already fighting red CI."
        )
        assert "always()" in condition, (
            f"Step {step.get('name')!r} lost always(). The runs most worth "
            "recording are the RED ones; a step that only runs on success "
            "records exactly the wrong half of the history."
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
