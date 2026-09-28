"""The pytest fast lane's per-test health reaches Trunk Flaky Tests, and only Trunk.

ADR-NEW-trunk-flaky-tests-sole-test-analytics (Mon 28 Sep 2026): Trunk Flaky Tests is
the sole test-analytics uploader;
Mergify CI Insights (ADR-0078) is gone. The upload is MEASURE ONLY: pytest's own
exit code is each shard's verdict, quarantine stays off, and a failed or empty
upload is annotated as UNMEASURED rather than read as a recorded run.

The `test` job stages each shard's JUnit report as an artifact; the isolated
`test-analytics` job, which never checks out repository code, downloads the five
artifacts and uploads them. That job boundary is what keeps TRUNK_API_TOKEN away
from the test suite: on a self-hosted runner a test can fork a process that
outlives its step and reads a later step's INPUT_TOKEN (Sol P1 on #3256).

Regression lines:
  - if the upload moves into a job that checks out repository code then
    TRUNK_API_TOKEN is readable by the test suite and anything it spawns
  - if the probe exports the token instead of a boolean then every later step
    can read it
  - if a Mergify upload or MERGIFY_TOKEN comes back then two tools disagree about
    the same runs and a second secret is in CI for nothing
  - if the upload step loses continue-on-error then a Trunk outage paints every
    pull request red on a lane that is only supposed to be measuring
  - if quarantine is switched on here then a third party can subtract real
    failures from the verdict, from a job that cannot even change it
  - if the action stops being pinned to a full commit SHA then a third party can
    change what runs inside this repo's CI without review
  - if use-uncloned-repo is dropped then the uploader looks for a git checkout
    this job deliberately does not have, and every upload fails
  - if the junit glob stops matching the staged per-shard reports then the upload
    sends nothing while the step still reads as having run
  - if the steps stop gating on the token then every run annotates before the
    secret exists, which is pure noise
  - if the guard stops counting report files then an upload given zero reports
    (the uploader allows missing files) reads as a success
  - if the guard stops separating a failed transfer from a shard with no report
    then the instrumentation dies silently behind a notice that says "expected"
  - if the pytest step stops writing a per-shard report under RUNNER_TEMP then a
    previous run's report can be staged as this run's measurement
  - if staging copies a report pytest did not write, reuses a directory, prunes
    another shard's directory, or cannot fail, then stale data is uploaded as
    current, or a write failure arrives as an expected absence
  - if the artifact name stops carrying the shard and the run attempt then a
    re-run downloads attempt 1's report and records it as the current attempt's
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

SHARD_JOB = "test"
ANALYTICS_JOB = "test-analytics"
PYTEST_STEP_ID = "pytest-shard"
STAGE_STEP_ID = "stage-insights"
PROBE_STEP_ID = "trunk-token"
UPLOAD_STEP_ID = "trunk-flaky-tests"
GUARD_NAME = "The Trunk upload must have recorded something"
SHARD_EXPR = "${{ matrix.shard }}"
SHARDS = (1, 2, 3, 4, 5)


# ----- workflow access --------------------------------------------------------
def _jobs() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def _steps(job: str) -> list[dict]:
    jobs = _jobs()
    assert job in jobs, f"ci.yml has no job {job!r}; jobs present: {sorted(jobs)}"
    return jobs[job]["steps"]


def _step_by_id(job: str, step_id: str) -> dict:
    matches = [s for s in _steps(job) if s.get("id") == step_id]
    assert len(matches) == 1, f"{job}: want one step id={step_id}, got {len(matches)}"
    return matches[0]


def _step_by_name(job: str, name: str) -> dict:
    matches = [s for s in _steps(job) if s.get("name") == name]
    assert len(matches) == 1, f"{job}: want one step {name!r}, got {len(matches)}"
    return matches[0]


def _uses(step: dict, action: str) -> bool:
    return str(step.get("uses", "")).startswith(f"{action}@")


def _artifact_upload() -> dict:
    uploads = [
        s
        for s in _steps(SHARD_JOB)
        if _uses(s, "actions/upload-artifact")
        and "ci-insights" in str(s.get("with", {}).get("name", ""))
    ]
    assert len(uploads) == 1, f"want one staged-report artifact upload, got {len(uploads)}"
    return uploads[0]


def _junitxml_path() -> str:
    match = re.search(r'--junitxml="([^"]+)"', _step_by_id(SHARD_JOB, PYTEST_STEP_ID)["run"])
    assert match, 'the pytest shard no longer writes --junitxml="<path>"'
    return match.group(1)


def _mentions_mergify_upload(text: str) -> bool:
    return "secrets.MERGIFY_TOKEN" in text or "mergifyio/gha-mergify-ci@" in text


# ----- the `test` job: writing and staging the report -------------------------
def test_pytest_writes_a_per_shard_report_where_no_earlier_run_can_reach() -> None:
    written = _junitxml_path()
    assert SHARD_EXPR in written, f"{written!r} does not vary per shard; shards race"
    # The runner empties RUNNER_TEMP at the start and end of every job, so a
    # report there cannot be left over from an earlier run.
    assert "RUNNER_TEMP" in written, f"{written!r} is not under RUNNER_TEMP"


def test_staging_copies_only_a_report_this_pytest_run_wrote() -> None:
    stage = _step_by_id(SHARD_JOB, STAGE_STEP_ID)
    run = stage["run"]
    assert f"steps.{PYTEST_STEP_ID}.outcome" in str(stage.get("env", {})), stage.get("env")
    # Anchored to the WHOLE case arm: `success|failure` is a substring of a
    # widened `success|failure|skipped|*)`, and a substring check let that
    # mutation through once.
    arm = re.search(r"^[ \t]*success\|failure\)[ \t]*$", run, re.MULTILINE)
    assert arm, "the copy is not gated on pytest having run (success or failure only)"
    cp_lines = [ln for ln in run.splitlines() if "cp " in ln]
    assert cp_lines and arm.start() < run.find("cp "), "the outcome gate must precede the copy"
    for line in cp_lines:
        assert "RUNNER_TEMP" in line, f"copies from somewhere pytest does not write: {line!r}"
    assert _junitxml_path().split("/", 1)[1] in run, "staging reads a different filename"


def test_staging_fails_when_a_required_copy_or_write_fails() -> None:
    """No errexit here, so each command a complete staging directory needs must
    refuse on its own, or a write failure arrives downstream as an absence."""
    run = _step_by_id(SHARD_JOB, STAGE_STEP_ID)["run"]
    assert "set -e" not in run, "no errexit, so the per-command guards are load-bearing"
    required = [ln.strip() for ln in run.splitlines() if "cp " in ln]
    for target in ('"$STAGE_DIR/outcome.txt"', '"$GITHUB_OUTPUT"'):
        writes = [ln.strip() for ln in run.splitlines() if target in ln and "printf" in ln]
        assert writes, f"expected a write to {target}"
        required += writes
    for line in required:
        assert line.endswith("|| {"), f"required, so it must fail the step: {line!r}"


def test_staging_failure_cannot_redden_the_lane() -> None:
    assert _step_by_id(SHARD_JOB, STAGE_STEP_ID).get("continue-on-error") is True


def test_the_staging_directory_is_unique_to_this_shard_run_and_attempt() -> None:
    stage = _step_by_id(SHARD_JOB, STAGE_STEP_ID)
    stage_dir = stage.get("env", {}).get("STAGE_DIR", "")
    for token in ("github.run_id", "github.run_attempt", SHARD_EXPR):
        assert token in stage_dir, f"STAGE_DIR {stage_dir!r} does not vary by {token}"
    assert stage_dir not in stage["run"], "read $STAGE_DIR; do not repeat the expression"


def test_the_prune_cannot_delete_another_shards_staging_directory() -> None:
    run = _step_by_id(SHARD_JOB, STAGE_STEP_ID)["run"]
    start = run.find("find .")
    assert start != -1, "no find-based prune in the staging step"
    invocation = run[start : run.find("\n", run.find("-exec", start))]
    assert SHARD_EXPR in invocation, f"prune is not scoped to this shard: {invocation!r}"
    assert "-mmin" in invocation, f"prune has no obsolescence proof: {invocation!r}"


def test_a_staging_failure_suppresses_the_artifact_upload() -> None:
    condition = str(_artifact_upload().get("if", ""))
    # `outcome`, not `conclusion`: continue-on-error rewrites conclusion to success.
    assert f"steps.{STAGE_STEP_ID}.outcome == 'success'" in condition, condition
    assert "always()" in condition, condition


def test_every_download_names_its_own_shards_artifact_for_this_attempt() -> None:
    up_name = _artifact_upload()["with"]["name"]
    assert SHARD_EXPR in up_name and "github.run_attempt" in up_name, up_name
    for shard in SHARDS:
        download = _step_by_id(ANALYTICS_JOB, f"download-{shard}")
        assert _uses(download, "actions/download-artifact"), download
        assert download["with"]["name"] == up_name.replace(SHARD_EXPR, str(shard)), (
            f"download-{shard} asks for {download['with']['name']!r}"
        )


# ----- the `test-analytics` job: isolation and the upload ---------------------
def test_the_token_never_shares_a_job_with_repository_code() -> None:
    jobs = _jobs()
    holders = sorted(n for n, j in jobs.items() if "secrets.TRUNK_API_TOKEN" in json.dumps(j))
    # Positive control built in: the analytics job must be found holding it.
    assert holders == [ANALYTICS_JOB], f"TRUNK_API_TOKEN is reachable from {holders}"
    assert not any(_uses(s, "actions/checkout") for s in _steps(ANALYTICS_JOB))
    carriers = [
        s.get("id") for s in _steps(ANALYTICS_JOB) if "secrets.TRUNK_API_TOKEN" in json.dumps(s)
    ]
    assert carriers == [PROBE_STEP_ID, UPLOAD_STEP_ID], carriers
    assert "TRUNK_API_TOKEN" not in json.dumps(jobs[ANALYTICS_JOB].get("env", {}))


def test_the_probe_exports_only_a_boolean() -> None:
    probe = _step_by_id(ANALYTICS_JOB, PROBE_STEP_ID)
    assert probe["env"] == {"TRUNK_API_TOKEN": "${{ secrets.TRUNK_API_TOKEN }}"}
    run = probe["run"]
    assert "present=true" in run and "present=false" in run, run
    assert "$TRUNK_API_TOKEN" not in run.replace("${TRUNK_API_TOKEN:-}", ""), run


def test_trunk_is_the_only_test_analytics_uploader() -> None:
    assert _mentions_mergify_upload("token: ${{ secrets.MERGIFY_TOKEN }}"), "positive control"
    assert not _mentions_mergify_upload(CI.read_text(encoding="utf-8"))
    uploaders = [s for s in _steps(ANALYTICS_JOB) if _uses(s, "trunk-io/analytics-uploader")]
    assert [s.get("id") for s in uploaders] == [UPLOAD_STEP_ID]


def test_the_upload_is_pinned_measure_only_and_non_fatal() -> None:
    upload = _step_by_id(ANALYTICS_JOB, UPLOAD_STEP_ID)
    assert re.fullmatch(r"trunk-io/analytics-uploader@[0-9a-f]{40}", upload["uses"])
    assert upload.get("continue-on-error") is True
    with_ = upload["with"]
    assert str(with_.get("quarantine")).lower() == "false", with_.get("quarantine")
    assert str(with_.get("use-uncloned-repo")).lower() == "true"


def test_the_junit_glob_matches_every_staged_shard_report() -> None:
    glob = _step_by_id(ANALYTICS_JOB, UPLOAD_STEP_ID)["with"]["junit-paths"]
    pattern = re.compile("^" + re.escape(glob).replace(r"\*", "[^/]*") + "$")
    written_name = PurePosixPath(_junitxml_path()).name
    for shard in SHARDS:
        path = _step_by_id(ANALYTICS_JOB, f"download-{shard}")["with"]["path"]
        report = f"{path}/{written_name.replace(SHARD_EXPR, str(shard))}"
        assert pattern.match(report), f"{glob!r} does not match {report!r}"
    assert not pattern.match("staged-1/outcome.txt"), "negative control"
    assert not pattern.match("junit-shard-1.xml"), "negative control"


def test_every_step_after_the_probe_is_skipped_without_a_token() -> None:
    gated = [s for s in _steps(ANALYTICS_JOB) if s.get("id") != PROBE_STEP_ID]
    assert len(gated) == len(SHARDS) + 2, [s.get("name") for s in gated]
    for step in gated:
        condition = str(step.get("if", ""))
        assert "steps.trunk-token.outputs.present == 'true'" in condition, step.get("name")
        assert "always()" in condition, f"{step.get('name')}: red runs must be recorded too"


def test_the_guard_reads_the_real_step_outcomes() -> None:
    env = _step_by_name(ANALYTICS_JOB, GUARD_NAME)["env"]
    assert env["TRUNK_UPLOAD"] == f"${{{{ steps.{UPLOAD_STEP_ID}.outcome }}}}"
    for shard in SHARDS:
        assert env[f"DOWNLOAD_{shard}"] == f"${{{{ steps.download-{shard}.outcome }}}}"


# ----- the guard, executed ----------------------------------------------------
def _run_guard(workdir: Path, upload: str, downloads: dict[int, str] | None = None) -> str:
    """Execute the guard's own run block, as the runner's default bash would."""
    bash = shutil.which("bash")
    assert bash, "bash is required to execute the guard"
    outcomes = {shard: "success" for shard in SHARDS} | (downloads or {})
    env = {"TRUNK_UPLOAD": upload, "PATH": "/usr/bin:/bin"}
    env |= {f"DOWNLOAD_{shard}": outcome for shard, outcome in outcomes.items()}
    done = subprocess.run(
        [bash, "-e", "-c", _step_by_name(ANALYTICS_JOB, GUARD_NAME)["run"]],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return done.stdout


def _stage(workdir: Path, rel: str, text: str = "<testsuites/>") -> None:
    (workdir / rel).parent.mkdir(parents=True, exist_ok=True)
    (workdir / rel).write_text(text)


@pytest.mark.parametrize("staged", [(), ("staged-1/outcome.txt",)])
def test_the_guard_reports_zero_report_files_as_unmeasured(
    tmp_path: Path, staged: tuple[str, ...]
) -> None:
    for rel in staged:
        _stage(tmp_path, rel, "success")
    out = _run_guard(tmp_path, "success")
    assert "::warning title=Trunk upload UNMEASURED::" in out, out
    assert "TRUNK_UPLOAD: success" not in out, out


def test_the_guard_reports_success_only_for_a_successful_upload(tmp_path: Path) -> None:
    for shard in SHARDS:
        _stage(tmp_path, f"staged-{shard}/junit-shard-{shard}.xml")
    out = _run_guard(tmp_path, "success")
    assert "TRUNK_UPLOAD: success (5 report files)" in out, out
    assert "UNMEASURED" not in out and "::notice" not in out, out
    failed = _run_guard(tmp_path, "failure")
    assert "::warning title=Trunk upload UNMEASURED::" in failed, failed
    assert "outcome=failure" in failed and "TRUNK_UPLOAD: success" not in failed, failed


def test_the_guard_tells_a_failed_transfer_from_a_shard_with_no_report(
    tmp_path: Path,
) -> None:
    for shard in (1, 4, 5):
        _stage(tmp_path, f"staged-{shard}/junit-shard-{shard}.xml")
    _stage(tmp_path, "staged-3/outcome.txt", "failure")
    out = _run_guard(tmp_path, "success", {2: "failure"})
    assert "::error title=Trunk upload UNMEASURED (shard 2 of 5)::" in out, out
    assert "download outcome=failure" in out, out
    assert "::notice title=Test analytics: no report (shard 3 of 5)::" in out, out
    for shard in (1, 4, 5):
        assert f"shard {shard} of 5" not in out, out
    assert "TRUNK_UPLOAD: success (3 report files)" in out, out
