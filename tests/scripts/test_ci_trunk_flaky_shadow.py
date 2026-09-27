"""The pytest fast lane's JUnit reports also reach Trunk Flaky Tests, as a SHADOW.

the maintainer, Sun 27 Sep 2026: evaluate Trunk alongside Mergify before any cutover.
The shadow reuses the reports the isolated `ci-insights` job already downloads,
so it adds no job and no extra billing minimum. It is MEASURE ONLY: Trunk's
quarantine (which can turn a failing test run green) stays off until the maintainer
decides otherwise, and the upload can never be the lane's verdict.

Regression lines:
  - if the Trunk upload moves into a job that checks out repository code then
    TRUNK_API_TOKEN is readable by the test suite and anything it spawns
  - if the upload step loses continue-on-error then a Trunk outage paints every
    pull request red on a lane that is only supposed to be measuring
  - if quarantine is switched on here then a third party can subtract real
    failures from the verdict before anyone has looked at the baseline
  - if the action stops being pinned to a full commit SHA then a third party
    can change what runs inside this repo's CI without review
  - if use-uncloned-repo is dropped then the uploader looks for a git checkout
    this job deliberately does not have, and every upload fails
  - if the junit glob stops matching the staged per-shard reports then the
    upload sends nothing while the step still reads as having run
  - if the guard is dropped then a failed upload reads the same as a success
  - if the steps stop gating on the token then every run annotates before the
    secret exists, which is pure noise
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
INSIGHTS_JOB = "ci-insights"
UPLOAD_STEP_ID = "trunk-flaky-tests"
PROBE_STEP_ID = "trunk-token"
GUARD_NAME = "The Trunk shadow upload must have run"


# -----------------------------------------------------------------------------
def _jobs() -> dict:
    return yaml.safe_load(CI.read_text())["jobs"]


def _steps(job: str) -> list[dict]:
    return _jobs()[job]["steps"]


def _step_by_id(job: str, step_id: str) -> dict:
    matches = [s for s in _steps(job) if s.get("id") == step_id]
    assert len(matches) == 1, f"{job}: want one step id={step_id}, got {len(matches)}"
    return matches[0]


def _step_by_name(job: str, name: str) -> dict:
    matches = [s for s in _steps(job) if s.get("name") == name]
    assert len(matches) == 1, f"{job}: want one step {name!r}, got {len(matches)}"
    return matches[0]


# -----------------------------------------------------------------------------
def test_trunk_upload_lives_in_the_isolated_insights_job() -> None:
    upload = _step_by_id(INSIGHTS_JOB, UPLOAD_STEP_ID)
    assert upload["uses"].startswith("trunk-io/analytics-uploader@"), upload["uses"]


def test_trunk_token_never_shares_a_job_with_repository_code() -> None:
    for name, job in _jobs().items():
        text = yaml.safe_dump(job)
        checks_out = any(
            str(s.get("uses", "")).startswith("actions/checkout@") for s in job.get("steps", [])
        )
        if checks_out:
            assert "TRUNK_API_TOKEN" not in text, f"{name} checks out code and sees the token"
    assert "TRUNK_API_TOKEN" in yaml.safe_dump(_jobs()[INSIGHTS_JOB]), "positive control"


def test_trunk_upload_cannot_redden_the_lane() -> None:
    assert _step_by_id(INSIGHTS_JOB, UPLOAD_STEP_ID).get("continue-on-error") is True


def test_trunk_quarantine_is_not_armed() -> None:
    with_ = _step_by_id(INSIGHTS_JOB, UPLOAD_STEP_ID)["with"]
    assert str(with_.get("quarantine")).lower() == "false", with_.get("quarantine")


def test_trunk_upload_is_pinned_to_a_sha() -> None:
    uses = _step_by_id(INSIGHTS_JOB, UPLOAD_STEP_ID)["uses"]
    assert re.fullmatch(r"trunk-io/analytics-uploader@[0-9a-f]{40}", uses), uses


def test_trunk_upload_runs_without_a_checkout() -> None:
    with_ = _step_by_id(INSIGHTS_JOB, UPLOAD_STEP_ID)["with"]
    assert str(with_.get("use-uncloned-repo")).lower() == "true"
    assert not any(
        str(s.get("uses", "")).startswith("actions/checkout@") for s in _steps(INSIGHTS_JOB)
    )


def test_trunk_junit_glob_matches_every_staged_shard_report() -> None:
    glob = _step_by_id(INSIGHTS_JOB, UPLOAD_STEP_ID)["with"]["junit-paths"]
    download_paths = [
        s["with"]["path"]
        for s in _steps(INSIGHTS_JOB)
        if str(s.get("id", "")).startswith("download-")
    ]
    assert len(download_paths) == 5, download_paths
    pattern = re.compile("^" + re.escape(glob).replace(r"\*", "[^/]*") + "$")
    for shard, path in enumerate(download_paths, start=1):
        report = f"{path}/junit-shard-{shard}.xml"
        assert pattern.match(report), f"{glob!r} does not match {report!r}"
    assert not pattern.match("staged-1/outcome.txt"), "negative control"


def test_trunk_steps_are_skipped_without_a_token() -> None:
    probe = _step_by_id(INSIGHTS_JOB, PROBE_STEP_ID)
    assert probe["env"] == {"TRUNK_API_TOKEN": "${{ secrets.TRUNK_API_TOKEN }}"}
    for step in (
        _step_by_id(INSIGHTS_JOB, UPLOAD_STEP_ID),
        _step_by_name(INSIGHTS_JOB, GUARD_NAME),
    ):
        assert "steps.trunk-token.outputs.present == 'true'" in step["if"], step["if"]


def test_trunk_guard_reports_a_failed_upload() -> None:
    guard = _step_by_name(INSIGHTS_JOB, GUARD_NAME)
    assert guard["env"]["TRUNK_UPLOAD"] == "${{ steps.trunk-flaky-tests.outcome }}"
    assert "::warning" in guard["run"] and "success" in guard["run"]
