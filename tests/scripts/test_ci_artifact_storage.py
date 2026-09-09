"""Artifact uploads cannot paint trunk red, and none outlives its use.

Trunk 5534d0e78 (Tue 8 Sep 2026 15:45 UTC) went red on "Upload this shard's
measured durations" alone: a 403 on FinalizeArtifact once Actions storage
crossed the plan's limit (8,097 live artifacts, 1.7 GB, most of them 7 to
14 day retentions of per-run byproducts). The durations are an input to the
next shard rebalance, not a verdict, so their upload is non-fatal and kept
for one day; every other per-run artifact keeps at most three days.

Regression lines:
  - if the durations upload can fail the shard then a storage block reds trunk
  - if any upload keeps more than 3 days then storage refills and the 403
    returns within a week at ~1000 artifacts per day
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
MAX_RETENTION_DAYS = 3
#: Uploads that ARE the job's deliverable: they must fail loud and may keep
#: their own retention. Everything else is a byproduct.
DELIVERABLE_UPLOADS = {
    ("macos-packaging.yml", "Upload the payload manifest"),
    ("macos-native-companion.yml", "Upload macOS production wheel"),
    ("release-check.yml", "Upload built dists"),
}


def _upload_steps(workflow: str) -> list[tuple[str, dict]]:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    return [
        (job, step)
        for job, spec in doc["jobs"].items()
        for step in spec.get("steps") or []
        if "actions/upload-artifact" in (step.get("uses") or "")
    ]


def _all_workflows() -> list[str]:
    """Every workflow file, discovered, so a new upload cannot escape the guard."""
    return sorted(p.name for p in WORKFLOWS.glob("*.yml"))


def test_the_durations_upload_is_non_fatal_and_short_lived() -> None:
    """[if] the durations upload can fail the shard or outlive a day [then] fail, [else stop]."""
    steps = [s for _, s in _upload_steps("ci.yml") if "durations" in (s.get("name") or "")]
    assert len(steps) == 1, [s.get("name") for s in steps]
    step = steps[0]
    assert step.get("continue-on-error") is True, "a storage block on this step must not red trunk"
    assert int(step["with"]["retention-days"]) == 1


def test_every_byproduct_upload_is_non_fatal() -> None:
    """[if] an upload that is not a job's verdict can fail the job [then] fail, [else stop]."""
    fatal = []
    for workflow in _all_workflows():
        for job, step in _upload_steps(workflow):
            if (workflow, step.get("name")) in DELIVERABLE_UPLOADS:
                assert step.get("continue-on-error") is not True, (
                    f"{workflow}: a deliverable must fail loud"
                )
                continue
            if step.get("continue-on-error") is not True:
                fatal.append(f"{workflow}:{job}:{step.get('name')}")
    assert not fatal, f"uploads that can red a green job on a storage 403: {fatal}"


def test_no_artifact_outlives_its_use() -> None:
    """[if] any upload keeps artifacts longer than 3 days [then] fail, [else stop]."""
    offenders = []
    checked = 0
    for workflow in _all_workflows():
        for job, step in _upload_steps(workflow):
            if (workflow, step.get("name")) in DELIVERABLE_UPLOADS:
                continue
            checked += 1
            days = step.get("with", {}).get("retention-days")
            if days is None or int(days) > MAX_RETENTION_DAYS:
                offenders.append(f"{workflow}:{job}:{step.get('name')} retention-days={days}")
    assert checked >= 6, f"expected the known uploads, saw {checked}"
    assert not offenders, offenders
