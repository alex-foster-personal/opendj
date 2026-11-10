"""The GUARD-8229 seam guard is wired where it can run, and nowhere it cannot.

test_frontend_unit_seam_gate.py shells out to
apps/webui/frontend/scripts/unit-test-seam-check.mjs, which imports the
`typescript` package from frontend node_modules. ci.yml's fast pytest lanes
never install node_modules, so they `--ignore` that file (the same treatment
as test_frontend_typing_gate.py), and ci.yml's `quality` job, which does
install them, collects it. An exclusion with no matching collection would
leave the guard's own regressions running nowhere while every board reads
green, which is the failure GUARD-8229 exists to stop.

These tests read only workflow YAML and package.json, so they run in the very
lanes that exclude the node-dependent file, and keep GUARD-8229 collected there.

[if] a fast lane drops a node-dependent test file [then] `quality` collects it, [else stop].

Regression lines:
  - if the fast lane ignores a tests/quality/test_frontend_*.py file that the
    `quality` job does not collect then that file runs nowhere, so broken
  - if the `frontend` job stops running check:unit-seams before the frontend
    unit tests then a missing seam is a TypeError again, so broken
  - if `test:unit`, `test:unit:slow` or `test:unit:all` stops chaining
    check:unit-seams ahead of `node --test` then a local run skips the guard,
    so broken
  - if the gating `frontend` job stops running the live escape-hatch hard zero
    then a compiler suppression reaches main unchecked (#10611), so broken
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.requirement("GUARD-8229")

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PACKAGE_JSON = REPO_ROOT / "apps" / "webui" / "frontend" / "package.json"
SEAM_GATE = "tests/quality/test_frontend_unit_seam_gate.py"
_FRONTEND_IGNORE = re.compile(r"--ignore=(tests/quality/test_frontend_\S+\.py)")


def _jobs() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def _runs(job: dict) -> list[str]:
    return [s.get("run") or "" for s in job["steps"]]


def _fast_lane_frontend_ignores(job: dict) -> set[str]:
    return {m for run in _runs(job) for m in _FRONTEND_IGNORE.findall(run)}


def _quality_collected(jobs: dict) -> set[str]:
    collected: set[str] = set()
    for run in _runs(jobs["quality"]):
        if "-m pytest" not in run and ".venv/bin/pytest" not in run:
            continue
        collected.update(re.findall(r"(?<![=\w/])(tests/quality/test_frontend_\S+\.py)", run))
    return collected


def test_every_node_dependent_exclusion_is_collected_by_quality() -> None:
    """If a frontend test file is ignored by a fast lane then `quality` must collect it."""
    jobs = _jobs()
    collected = _quality_collected(jobs)
    assert collected, "the quality job collects no tests/quality/test_frontend_*.py file at all"
    for job_id in ("test", "fast"):
        ignored = _fast_lane_frontend_ignores(jobs[job_id])
        assert SEAM_GATE in ignored, (
            f"ci.yml `{job_id}` must --ignore {SEAM_GATE}: it imports node_modules/typescript, "
            "which that lane never installs"
        )
        orphaned = sorted(ignored - collected)
        assert not orphaned, (
            f"ci.yml `{job_id}` ignores {orphaned}, but the `quality` job never collects them, so they run nowhere"
        )


def test_frontend_job_checks_seams_before_unit_tests() -> None:
    """If check:unit-seams leaves the frontend job or follows the unit tests, seams go unchecked."""
    runs = _runs(_jobs()["frontend"])
    seam = [i for i, run in enumerate(runs) if "pnpm run check:unit-seams" in run]
    unit = [i for i, run in enumerate(runs) if re.search(r"pnpm (run )?test:unit\b", run)]
    assert seam, "ci.yml `frontend` job no longer runs pnpm run check:unit-seams"
    assert unit, "ci.yml `frontend` job no longer runs the frontend unit tests"
    assert seam[0] < unit[0], "check:unit-seams must run before the frontend unit tests"


@pytest.mark.parametrize("script", ["test:unit", "test:unit:slow", "test:unit:all"])
def test_unit_scripts_chain_the_seam_check_first(script: str) -> None:
    """If a unit-test script drops check:unit-seams before node --test, a local run skips it."""
    scripts = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["scripts"]
    assert scripts["check:unit-seams"] == "node scripts/unit-test-seam-check.mjs"
    command = scripts[script]
    assert "pnpm run check:unit-seams &&" in command, f"{script} no longer runs the seam check"
    node_test = command.index("node --experimental-strip-types --test")
    assert command.index("check:unit-seams") < node_test, f"{script} must run the seam check before node --test"


ESCAPE_HATCH_TEST = "tests/quality/test_frontend_typing_gate.py::test_live_frontend_tree_has_no_escape_hatches"


def test_frontend_job_runs_the_escape_hatch_hard_zero() -> None:
    """If the gating frontend job drops the live escape-hatch test, a suppression reaches main."""
    runs = _runs(_jobs()["frontend"])
    hits = [run for run in runs if ESCAPE_HATCH_TEST in run]
    assert hits, f"ci.yml `frontend` job no longer runs {ESCAPE_HATCH_TEST}"
    assert all("pytest" in run for run in hits), "the escape-hatch step must run it under pytest"
