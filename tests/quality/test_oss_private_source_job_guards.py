"""Private-infrastructure jobs skip outside the private source repo.

[if] a job needs Trunk, the Semgrep AppSec Platform token or the billing token
[then] its job-level `if:` carries `endsWith(github.repository, '/music-dj-tools')`
as a top-level conjunct, [else stop].

The public mirror (`opendj`) and its forks run on GitHub-hosted runners with no
Trunk account and no Semgrep app token, so these jobs must read as skipped there,
not failed. The publish recipe rewrites the owner login but never the repo name
`music-dj-tools`, so `endsWith` on the name is true here and false in the mirror;
an owner-based test (`github.repository_owner == ...`) would be rewritten into
something that no longer matches here either, and is refused below.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.ci_cost_guard_workflow_reader import top_level_disjuncts

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
GUARD = "endsWith(github.repository, '/music-dj-tools')"

# The trunk-*.yml workflows that once sat here were deleted on Wed 4 Nov 2026
# (ADR-NEW-trunk-removed-from-pr-ci), so no Trunk job needs the guard.
GUARDED_JOBS = sorted(
    [
        ("ci-budget-watch.yml", "watch"),
        ("security.yml", "semgrep"),
    ]
)
# Token-less security jobs keep running in the mirror (Semgrep CE, osv, trufflehog).
UNGUARDED_JOBS = sorted([("security.yml", "pr"), ("security.yml", "scheduled")])


def _job_if(workflow: str, job: str) -> str:
    path = WORKFLOWS / workflow
    assert path.is_file(), f"{workflow} is missing"
    jobs = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]
    assert job in jobs, f"{workflow} has no job {job!r}"
    return " ".join(str(jobs[job].get("if", "")).split())


def _top_level_conjuncts(condition: str) -> list[str]:
    """`condition` split on `&&` at bracket depth zero."""
    parts, depth, start = [], 0, 0
    for index, char in enumerate(condition):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "&" and depth == 0 and condition[index : index + 2] == "&&":
            parts.append(condition[start:index].strip())
            start = index + 2
    parts.append(condition[start:].strip())
    return [part for part in parts if part]


def _guarded(condition: str) -> bool:
    # A top-level `||` would let a disjunct bypass the guard, so the whole condition
    # must be one disjunct, and the guard one of its top-level conjuncts.
    return len(top_level_disjuncts(condition)) == 1 and GUARD in _top_level_conjuncts(condition)


@pytest.mark.parametrize(("workflow", "job"), GUARDED_JOBS)
def test_private_only_job_skips_outside_the_private_source_repo(workflow: str, job: str) -> None:
    condition = _job_if(workflow, job)
    assert _guarded(condition), f"{workflow}:{job} lacks a top-level {GUARD}: {condition!r}"
    assert "repository_owner" not in condition, f"{workflow}:{job} keys on the owner, which the publish recipe rewrites"


@pytest.mark.parametrize(("workflow", "job"), UNGUARDED_JOBS)
def test_token_less_security_job_still_runs_in_the_mirror(workflow: str, job: str) -> None:
    """Overshoot control: guarding every security job would silence the CE scans too."""
    assert GUARD not in _job_if(workflow, job)


def test_the_guard_detector_fires_both_ways() -> None:
    """Controls: the detector accepts a guarded condition and refuses the bypass shapes."""
    assert _guarded(f"a == 'b' && {GUARD}")
    assert _guarded(f"{GUARD} && (a || b)")
    assert not _guarded("a == 'b'")
    assert not _guarded(f"{GUARD} && a || b")
    assert not _guarded(f"(a || {GUARD}) && c")
