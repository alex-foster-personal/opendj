"""The semgrep job checks out shallow; the scan fetches its own base and head.

Reads the shipped workflow, not a second hand-maintained representation.

Regression lines:
  - if the semgrep job's checkout sets fetch-depth again, then broken (a median
    22 s clone that `semgrep ci` does not use, billed as a whole hosted minute on
    5 of 13 measured PR runs)
  - if the semgrep step loses the git credential it fetches base and head with,
    then broken (the shallow checkout rests on that fetch working)
  - if the pr job's checkout stops fetching full history, then broken (gitleaks'
    commit range and the Semgrep CE baseline need it; this is the deliberate
    contrast, not an oversight)
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SECURITY = REPO / ".github" / "workflows" / "security.yml"
CHECKOUT_PREFIX = "actions/checkout@"


def _workflow() -> dict:
    assert SECURITY.is_file(), "security workflow is missing"
    document = yaml.safe_load(SECURITY.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "security.yml is not a mapping"
    return document


def _checkout_with(job: dict) -> dict:
    checkouts = [s for s in job["steps"] if str(s.get("uses", "")).startswith(CHECKOUT_PREFIX)]
    assert len(checkouts) == 1, f"expected exactly one checkout step, got {len(checkouts)}"
    return checkouts[0].get("with") or {}


def test_semgrep_job_checks_out_shallow() -> None:
    with_ = _checkout_with(_workflow()["jobs"]["semgrep"])
    assert "fetch-depth" not in with_, (
        f"semgrep job checkout sets fetch-depth={with_['fetch-depth']!r}; semgrep ci "
        "fetches base and head itself, so this only adds a billed minute"
    )
    assert with_.get("persist-credentials") is False


def test_semgrep_step_keeps_its_own_fetch_credential() -> None:
    steps = _workflow()["jobs"]["semgrep"]["steps"]
    semgrep = next(s for s in steps if s.get("name") == "semgrep ci")
    env = semgrep.get("env") or {}
    assert "FETCH_TOKEN" in env, "semgrep ci step no longer receives FETCH_TOKEN"
    assert "GIT_CONFIG_KEY_0" in str(semgrep.get("run")), (
        "semgrep ci step no longer wires FETCH_TOKEN into git; the shallow checkout "
        "depends on semgrep fetching base and head itself"
    )


def test_pr_job_still_fetches_full_history() -> None:
    """CONTROL and contrast: the pr job needs history and keeps fetch-depth 0."""
    with_ = _checkout_with(_workflow()["jobs"]["pr"])
    assert with_.get("fetch-depth") == 0, f"pr job checkout lost fetch-depth 0: {with_}"
