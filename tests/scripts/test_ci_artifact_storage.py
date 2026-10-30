"""CI artifacts live on Cloudflare R2, never in GitHub Actions artifact storage.

Trunk 5534d0e78 (Tue 8 Sep 2026) went red on a 403 from FinalizeArtifact once Actions
storage crossed the plan's limit, and on Fri 30 Oct 2026 GitHub blocked every artifact
upload on the account outright: on Free and Pro, artifacts and Packages share a 0.5 to
1 GB pool billed in GB-hours, so deleting artifacts does not unblock it. Every upload
now goes to R2 through .github/actions/r2-artifact (retention: the bucket's 14-day
lifecycle rule on ci/, set on the bucket, not in a workflow).

Regression lines:
  - if any workflow uses a GitHub artifact action then the account quota can block CI again
  - if the durations upload can fail the shard then a storage outage reds trunk
  - if a deliverable upload is non-fatal then a missing deliverable reads green
  - if the action leaves macOS on the interpreter's own CA store then a python.org build
    fails every R2 call with CERTIFICATE_VERIFY_FAILED (mac-megamac, Fri 30 Oct 2026)
  - if a job cannot see the action or its client then the upload never runs, and
    continue-on-error reads that as success (ci-budget-watch, Fri 30 Oct 2026)
  - if a workflow sets the product's R2_* names then CI's artifact credentials arm the
    live R2 tests and the engine's cloud path in every pytest step
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
ACTION = REPO / ".github" / "actions" / "r2-artifact" / "action.yml"
R2_ACTION = "./.github/actions/r2-artifact"
R2_ACTION_DIR = ".github/actions"
R2_CLIENT_DIR = "scripts"
#: A deliverable may only skip a missing file once the job has already failed.
MISSING_DELIVERABLE_VALUES = ("error", "${{ job.status == 'success' && 'error' || 'warn' }}")
#: The product's own R2 settings. CI's artifact bucket uses CI_ARTIFACT_R2_* so that a
#: workflow-wide env cannot reach them: #7910 set these names and failed 3 tests on main.
PRODUCT_R2_ENV = {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"}
GITHUB_ARTIFACT_ACTIONS = (
    "actions/upload-artifact",
    "actions/download-artifact",
    "actions/upload-pages-artifact",
    "actions/deploy-pages",
)
#: Uploads that ARE the job's deliverable: they must fail loud and may keep
#: their own retention. Everything else is a byproduct.
DELIVERABLE_UPLOADS = {
    ("macos-packaging.yml", "Upload the payload manifest"),
    ("macos-native-companion.yml", "Upload macOS production wheel"),
    ("release-check.yml", "Upload built dists"),
    ("ci.yml", "Upload production frontend artifact"),
    # The published ledger IS the job: ci.yml installs it, so a silent failure
    # here would leave CI splitting on a stale seed with nothing red.
    ("durations-ledger.yml", "Publish the ledger for ci.yml"),
}


def _upload_steps(workflow: str) -> list[tuple[str, dict]]:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    return [
        (job, step)
        for job, spec in doc["jobs"].items()
        for step in spec.get("steps") or []
        if step.get("uses") == R2_ACTION and (step.get("with") or {}).get("mode") == "upload"
    ]


def _all_workflows() -> list[str]:
    """Every workflow file, discovered, so a new upload cannot escape the guard."""
    return sorted(p.name for p in WORKFLOWS.glob("*.yml"))


def _sparse_entries(step: dict) -> list[str] | None:
    sparse = (step.get("with") or {}).get("sparse-checkout")
    return None if sparse is None else [line.strip() for line in sparse.splitlines() if line.strip()]


def _cone_holds(cone: list[str], directory: str) -> bool:
    return any(directory == entry or directory.startswith(f"{entry}/") for entry in cone)


def _r2_step_blindness(job: str, steps: list[dict]) -> list[str]:
    """Why a job's R2 steps cannot find the action or its client, empty when they can."""
    uses = [str(step.get("uses") or "") for step in steps]
    r2_steps = [i for i, used in enumerate(uses) if used == R2_ACTION]
    checkouts = [i for i, used in enumerate(uses) if used.startswith("actions/checkout")]
    if not r2_steps:
        return []
    if not checkouts or checkouts[0] > r2_steps[0]:
        return [f"{job}: no checkout before its first R2 step"]
    cone = _sparse_entries(steps[checkouts[0]])
    if cone is None:
        return []
    return [f"{job}: sparse cone omits {d}" for d in (R2_ACTION_DIR, R2_CLIENT_DIR) if not _cone_holds(cone, d)]


def test_a_blind_job_is_reported() -> None:
    """[if] the blindness check cannot flag a known-blind job [then] the scan below proves nothing, [else stop]."""
    r2_step = {"uses": R2_ACTION}
    checkout = {"uses": "actions/checkout@sha"}
    sparse = {"uses": "actions/checkout@sha", "with": {"sparse-checkout": "scripts\n.github/workflows\n"}}
    full_cone = {"uses": "actions/checkout@sha", "with": {"sparse-checkout": "scripts\n.github/actions\n"}}
    assert _r2_step_blindness("j", [r2_step, checkout]) == ["j: no checkout before its first R2 step"]
    assert _r2_step_blindness("j", [sparse, r2_step]) == ["j: sparse cone omits .github/actions"]
    assert _r2_step_blindness("j", [checkout, r2_step]) == []
    assert _r2_step_blindness("j", [full_cone, r2_step]) == []


def test_every_r2_step_can_see_the_action_and_its_client() -> None:
    """[if] a job runs an R2 step without the action or client on disk [then] fail, [else stop]."""
    blind = []
    for workflow in _all_workflows():
        doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
        for job, spec in doc["jobs"].items():
            blind += [f"{workflow}:{why}" for why in _r2_step_blindness(job, spec.get("steps") or [])]
    assert not blind, blind


def test_no_workflow_uses_github_artifact_storage() -> None:
    """[if] any workflow step uses a GitHub artifact action [then] fail, [else stop]."""
    offenders = []
    for workflow in _all_workflows():
        doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
        for job, spec in doc["jobs"].items():
            for step in spec.get("steps") or []:
                uses = step.get("uses") or ""
                if uses.startswith(GITHUB_ARTIFACT_ACTIONS):
                    offenders.append(f"{workflow}:{job}:{step.get('name')} uses {uses}")
    assert not offenders, offenders


def test_the_r2_uploads_exist() -> None:
    """[if] the scan above finds no R2 uploads [then] it is scanning nothing, so fail, [else stop]."""
    uploads = [step for workflow in _all_workflows() for _, step in _upload_steps(workflow)]
    assert len(uploads) >= 20, f"expected the known R2 uploads, saw {len(uploads)}"


def test_the_durations_upload_is_non_fatal() -> None:
    """[if] a durations step can red the shard [then] fail, [else stop]."""
    steps = [(job, s) for job, s in _upload_steps("ci.yml") if "durations" in (s.get("name") or "")]
    assert steps, "no step named for a shard durations upload exists in ci.yml"
    for job, step in steps:
        assert step.get("continue-on-error") is True, (
            f"ci.yml:{job}:{step.get('name')} is missing continue-on-error: true -- "
            "an input to the next rebalance is not a verdict"
        )


def test_every_byproduct_upload_is_non_fatal_and_every_deliverable_fails_loud() -> None:
    """[if] a byproduct upload can fail a job, or a deliverable upload cannot [then] fail, [else stop]."""
    wrong = []
    for workflow in _all_workflows():
        for job, step in _upload_steps(workflow):
            deliverable = (workflow, step.get("name")) in DELIVERABLE_UPLOADS
            if deliverable and step.get("continue-on-error") is True:
                wrong.append(f"{workflow}:{job}:{step.get('name')} is a deliverable but non-fatal")
            elif not deliverable and step.get("continue-on-error") is not True:
                wrong.append(f"{workflow}:{job}:{step.get('name')} is a byproduct but fatal")
    assert not wrong, wrong


def test_the_action_gives_macos_the_system_ca_bundle() -> None:
    """[if] the action stops pointing macOS at the system CA bundle [then] fail, [else stop]."""
    run = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"][0]["run"]
    assert '[ "$(uname)" != Darwin ] || export SSL_CERT_FILE="${SSL_CERT_FILE:-/etc/ssl/cert.pem}"' in run
    assert run.index("SSL_CERT_FILE") < run.index('case "$INPUT_MODE"'), "the bundle must be set before the client runs"


def test_a_green_job_cannot_lose_its_deliverable() -> None:
    """[if] a green job can upload nothing and still pass [then] fail, [else stop]."""
    found = []
    for workflow in _all_workflows():
        for job, step in _upload_steps(workflow):
            if (workflow, step.get("name")) in DELIVERABLE_UPLOADS:
                found.append(step)
                missing = (step.get("with") or {}).get("if-no-files-found", "error")
                assert missing in MISSING_DELIVERABLE_VALUES, f"{workflow}:{job}:{step.get('name')} reads {missing!r}"
    assert found, "no deliverable upload was checked"


def _env_blocks(workflow: dict) -> list[dict]:
    jobs = (workflow.get("jobs") or {}).values()
    steps = [step for job in jobs for step in job.get("steps") or []]
    return [block.get("env") or {} for block in (workflow, *jobs, *steps)]


def test_no_workflow_sets_the_products_r2_names() -> None:
    leaks = {
        path.name: sorted(PRODUCT_R2_ENV & set(env))
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for env in _env_blocks(yaml.safe_load(path.read_text(encoding="utf-8")))
        if PRODUCT_R2_ENV & set(env)
    }
    assert not leaks, f"product R2 settings set in CI, use CI_ARTIFACT_R2_*: {leaks}"
