"""The `merge window` job's `gh` must satisfy the version guard's floor (#2883).

Every scheduled `periodic-checks.yml` run from Fri 12 Sep 2026 onward died in
the `window` job: `scripts/periodic_window.py`'s `Gh.api_json_paginated` runs
`gh api --paginate --slurp`, which needs gh 2.64.0
(`scripts/gh_version_guard.py`), and the CI_RUNS_ON_LINUX (agentbox) runners
ran gh 2.62.0. `require_gh_min_version()` (landed 7cbf1ac8b, Mon 14 Sep 2026)
turned the opaque `unknown flag: --slurp` into a named failure, but a named
failure is still a failure: every job gated on `window` still skipped.

This module tests the OTHER half: the `window` job installs a pinned `gh`
binary, at or above the guard's own floor, before it runs anything that calls
`gh`. A version pin that drifted below the guard's `MIN_GH_VERSION` would
reintroduce the exact outage this fixes while looking like a fix, so the
floor is read from `scripts.gh_version_guard` rather than restated as a
literal here (verification.md: prefer invariants to values).

-Claude
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from scripts.gh_version_guard import MIN_GH_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
PERIODIC = REPO_ROOT / ".github" / "workflows" / "periodic-checks.yml"


def _periodic() -> dict:
    return yaml.safe_load(PERIODIC.read_text(encoding="utf-8"))


def _window_job() -> dict:
    jobs = _periodic().get("jobs") or {}
    job = jobs.get("window")
    assert job is not None, "periodic-checks.yml has no `window` job"
    return job


def _step_names(job: dict) -> list[str]:
    return [step.get("name", "") for step in job.get("steps", [])]


def _find_step(job: dict, *, name_pattern: str) -> dict:
    for step in job.get("steps", []):
        if re.search(name_pattern, step.get("name", ""), re.IGNORECASE):
            return step
    raise AssertionError(
        f"no step in `window` job matches {name_pattern!r}; "
        f"steps present: {_step_names(job)}"
    )


def _pinned_gh_version(step: dict) -> tuple[int, int, int]:
    env = step.get("env") or {}
    raw = env.get("GH_CLI_VERSION")
    assert raw, (
        "the gh-install step must pin its version via env.GH_CLI_VERSION, "
        f"got env={env!r}"
    )
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(raw))
    assert match, f"GH_CLI_VERSION {raw!r} is not a plain major.minor.patch pin"
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def test_window_job_installs_gh_before_computing_the_window() -> None:
    job = _window_job()
    names = _step_names(job)
    install_idx = next(
        (i for i, n in enumerate(names) if re.search(r"install.*gh\b", n, re.IGNORECASE)),
        None,
    )
    compute_idx = next(
        (i for i, n in enumerate(names) if re.search(r"compute the window", n, re.IGNORECASE)),
        None,
    )
    assert install_idx is not None, (
        f"`window` job has no gh-install step; steps present: {names}"
    )
    assert compute_idx is not None, f"`window` job has no 'Compute the window' step: {names}"
    assert install_idx < compute_idx, (
        "the gh-install step must run BEFORE 'Compute the window', which is "
        "the step that actually calls `gh api --paginate --slurp`"
    )


def test_pinned_gh_version_meets_the_guards_floor() -> None:
    job = _window_job()
    step = _find_step(job, name_pattern=r"install.*gh\b")
    pinned = _pinned_gh_version(step)
    assert pinned >= MIN_GH_VERSION, (
        f"periodic-checks.yml pins gh {'.'.join(map(str, pinned))}, below "
        f"scripts.gh_version_guard.MIN_GH_VERSION "
        f"{'.'.join(map(str, MIN_GH_VERSION))} -- this would reproduce the "
        "exact `unknown flag: --slurp` outage the guard exists to catch"
    )


def test_window_job_proves_the_installed_gh_version() -> None:
    job = _window_job()
    names = _step_names(job)
    assert any(re.search(r"verify gh version", n, re.IGNORECASE) for n in names), (
        f"`window` job never prints `gh --version` as evidence; steps present: {names}"
    )
    verify = _find_step(job, name_pattern=r"verify gh version")
    assert "gh --version" in (verify.get("run") or ""), (
        "the verify step must actually invoke `gh --version`, not just be named for it"
    )


def test_gh_install_step_runs_before_setup_python_dependent_step() -> None:
    """The install step must land its binary on PATH via $GITHUB_PATH.

    A step that downloads gh into a temp dir but never appends it to
    $GITHUB_PATH leaves later steps resolving whatever `gh` the runner image
    shipped -- silently reproducing the outage while looking installed.
    """
    job = _window_job()
    step = _find_step(job, name_pattern=r"install.*gh\b")
    run = step.get("run") or ""
    assert "GITHUB_PATH" in run, (
        "the gh-install step must append its install dir to $GITHUB_PATH so "
        "subsequent steps (and `gh --version`) resolve the pinned binary"
    )
