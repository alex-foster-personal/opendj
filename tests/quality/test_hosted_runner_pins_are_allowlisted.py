"""No workflow job runs on a GitHub-hosted runner unless it is allowlisted with a reason.

Thu 15 Oct 2026: the hosted Actions budget ran out, every job pinned to a hosted
label failed to start, and main stayed red for about 10 hours
(ADR-NEW-hosted-runner-exit). Self-hosted capacity is the default. A hosted pin
is a decision, so it has to be written down here, next to its reason.

THE RULE, per job in .github/workflows/*.yml|yaml (reusable-workflow callers,
which carry no runs-on of their own, are skipped):
  - a literal runs-on (string or list) that names a GitHub-hosted image label
    (ubuntu-*, macos-*, windows-*) and not `self-hosted` is a HOSTED PIN;
  - an expression runs-on must route through a `vars.CI_RUNS_ON_*` variable.
    Any other expression cannot be classified by reading the file, so it is a
    pin too, and needs an allowlist entry (the runner-canary vendor matrix);
  - every hosted pin must be in ALLOWED_HOSTED with a non-empty reason;
  - every allowlist entry must name a job that exists, so an entry cannot
    silently outlive its job. An entry for a job that is no longer hosted is
    reported but NOT failed: a PR that moves one off hosted must not redden
    a PR that only reads the list.

Regression lines (each one an assertion below):
  - if a new job pinned to ubuntu-latest passes then broken
  - if a literal [macos-14] list passes unlisted then broken
  - if an expression with no vars.CI_RUNS_ON_ route passes unlisted then broken
  - if a vars.CI_RUNS_ON_* route or a [self-hosted, ...] list is flagged then broken
  - if an allowlist entry names a job that does not exist and passes then broken
  - if the scan finds zero jobs then broken (an empty scan is not a clean scan)
  - if a hyphenated hosted label (ubuntu-24.04-arm, macos-14-large) passes then broken
  - if a routed expression that can still select a hosted literal while its variable
    is set passes then broken (only the trailing unset-variable fallback is allowed)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO / ".github" / "workflows"
HOSTED_LABEL = re.compile(r"^(?:ubuntu|macos|windows)-[\w.-]+$")
#: A quoted hosted label inside an expression, e.g. '"ubuntu-latest"' or 'macos-14-large'.
HOSTED_LITERAL = re.compile(r"'\"?(?:ubuntu|macos|windows)-[\w.-]+\"?'")
#: The one place a hosted literal may sit in a routed expression: the trailing fallback
#: used when the variable is unset, `... || '"ubuntu-latest"') }}`.
TRAILING_FALLBACK = re.compile(r"\|\|\s*'\"?(?:ubuntu|macos|windows)-[\w.-]+\"?'\s*\)\s*\}\}\s*$")
ROUTED = re.compile(r"\bvars\.CI_RUNS_ON_[A-Z0-9_]+\b")

_TOKEN_ON_EPHEMERAL_VM = (
    "holds secrets.TRUNK_API_TOKEN with no checkout; a fresh hosted VM keeps the org "
    "token away from processes left by repository code on shared self-hosted runners "
    "(pinned by its own test)"
)

#: (workflow file, job id) -> why this job may stay on a GitHub-hosted runner.
ALLOWED_HOSTED: dict[tuple[str, str], str] = {
    ("security.yml", "semgrep"): (
        "runs the pinned semgrep container with the AppSec Platform token; "
        "ADR-NEW-security-scans-on-self-hosted-pool keeps it hosted"
    ),
    ("runner-canary.yml", "pytest"): (
        "third-party vendor runners chosen by the budget gate's label map, by design "
        "(ADR-NEW-runner-canary); no pull_request trigger"
    ),
    ("track-catcher-core.yml", "core"): (
        "needs a Swift toolchain for `swift test` against the real engine; the self-hosted "
        "Linux pool has no swift, so this job stays on the GitHub-hosted image that ships it"
    ),
}


def _labels(runs_on: object) -> list[str]:
    if isinstance(runs_on, str):
        return [runs_on]
    if isinstance(runs_on, list):
        return [str(label) for label in runs_on]
    if isinstance(runs_on, dict):  # {group:, labels:}
        return _labels(runs_on.get("labels", []))
    raise AssertionError(f"unreadable runs-on {runs_on!r}")


def is_hosted_pin(runs_on: object) -> bool:
    """True when this runs-on can be satisfied only by a GitHub-hosted runner, or cannot be classified."""
    if isinstance(runs_on, str) and "${{" in runs_on:
        if not ROUTED.search(runs_on):
            return True
        # Routed, but a hosted literal anywhere except the unset-variable fallback means
        # the expression can pick a hosted label while the variable IS set.
        return bool(HOSTED_LITERAL.search(TRAILING_FALLBACK.sub(")}}", runs_on)))
    labels = _labels(runs_on)
    if "self-hosted" in labels:
        return False
    return any(HOSTED_LABEL.match(label) for label in labels)


def workflow_jobs(workflow_dir: Path = WORKFLOW_DIR) -> dict[tuple[str, str], dict]:
    jobs: dict[tuple[str, str], dict] = {}
    for path in sorted([*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")]):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict), f"{path.name} is not a mapping"
        for job_id, job in (document.get("jobs") or {}).items():
            jobs[(path.name, job_id)] = job
    return jobs


def unlisted_hosted_pins(jobs: dict[tuple[str, str], dict], allowed: dict[tuple[str, str], str]) -> list[str]:
    problems = []
    for key, job in jobs.items():
        if "runs-on" not in job:
            assert "uses" in job, f"{key} has neither runs-on nor uses"
            continue
        if is_hosted_pin(job["runs-on"]) and not allowed.get(key, "").strip():
            problems.append(f"{key[0]}:{key[1]} runs-on {job['runs-on']!r}")
    return problems


# ----- the live tree


def test_the_scan_reads_real_jobs() -> None:
    jobs = workflow_jobs()
    assert len(jobs) >= 50, f"only {len(jobs)} jobs found; the scan is not reading the workflows"
    assert any(is_hosted_pin(job.get("runs-on", "self-hosted")) for job in jobs.values()), (
        "no hosted pin found at all; the classifier cannot see the allowlisted trunk-token jobs it must see"
    )


def test_every_hosted_pin_is_allowlisted_with_a_reason() -> None:
    problems = unlisted_hosted_pins(workflow_jobs(), ALLOWED_HOSTED)
    assert not problems, (
        "GitHub-hosted runner pins with no ALLOWED_HOSTED entry. Route the job through "
        "${{ fromJSON(vars.CI_RUNS_ON_BIFROST1 || vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }} "
        "or allowlist it with a reason:\n  " + "\n  ".join(problems)
    )


def test_every_allowlist_entry_names_a_job_that_exists() -> None:
    jobs = workflow_jobs()
    missing = [f"{f}:{j}" for f, j in ALLOWED_HOSTED if (f, j) not in jobs]
    assert not missing, f"allowlist entries for jobs that do not exist: {missing}"
    no_longer_hosted = [f"{f}:{j}" for (f, j) in ALLOWED_HOSTED if not is_hosted_pin(jobs[(f, j)].get("runs-on", ""))]
    if no_longer_hosted:
        print(f"[WARN] allowlisted but no longer hosted (prune these): {no_longer_hosted}")


# ----- the classifier, against fixtures (mutation controls)


@pytest.mark.parametrize(
    "runs_on",
    [
        "ubuntu-latest",
        "ubuntu-24.04",
        ["macos-14"],
        "windows-latest",
        "ubuntu-24.04-arm",
        "macos-14-large",
        ["windows-2025-vs2026"],
        "${{ matrix.os }}",
        "${{ vars.CI_RUNS_ON_LINUX && '\"ubuntu-latest\"' }}",
        "${{ fromJSON(vars.CI_RUNS_ON_LINUX && '\"macos-14\"' || '\"ubuntu-latest\"') }}",
        "${{ fromJSON(needs.gate.outputs.labels)[matrix.vendor] }}",
    ],
)
def test_a_hosted_or_unclassifiable_pin_is_caught(runs_on: object) -> None:
    assert is_hosted_pin(runs_on)
    jobs = {("new.yml", "fresh"): {"runs-on": runs_on, "steps": []}}
    assert unlisted_hosted_pins(jobs, ALLOWED_HOSTED) == [f"new.yml:fresh runs-on {runs_on!r}"]


@pytest.mark.parametrize(
    "runs_on",
    [
        "${{ fromJSON(vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}",
        "${{ fromJSON(vars.CI_RUNS_ON_BIFROST1 || vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}",
        ["self-hosted", "macOS", "ARM64", "airgap-mac"],
        ["self-hosted", "linux", "ubuntu-latest"],
    ],
)
def test_a_self_hosted_route_is_not_flagged(runs_on: object) -> None:
    assert not is_hosted_pin(runs_on)


def test_an_allowlisted_pin_needs_a_reason() -> None:
    jobs = {("x.yml", "j"): {"runs-on": "ubuntu-latest"}}
    assert unlisted_hosted_pins(jobs, {("x.yml", "j"): "  "}) == ["x.yml:j runs-on 'ubuntu-latest'"]
    assert unlisted_hosted_pins(jobs, {("x.yml", "j"): "a reason"}) == []


def test_a_new_ubuntu_latest_job_in_a_real_workflow_goes_red(tmp_path: Path) -> None:
    """Mutation control on a copy of the real tree: append one hosted job and the scan must name it."""
    for path in WORKFLOW_DIR.glob("*.y*ml"):
        (tmp_path / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    target = tmp_path / "pr-body-gates.yml"
    target.write_text(
        target.read_text(encoding="utf-8")
        + "\n  sneaky:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n",
        encoding="utf-8",
    )
    problems = unlisted_hosted_pins(workflow_jobs(tmp_path), ALLOWED_HOSTED)
    assert problems == ["pr-body-gates.yml:sneaky runs-on 'ubuntu-latest'"], problems
