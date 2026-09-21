"""The playlist-switch bench runs in the e2e extended lane, never in the pytest lanes.

``tests/perf/test_library_playlist_switch_bench.py`` is a pytest test that drives a full
Playwright bench (frontend build, ``pnpm exec playwright test``, a real engine, ten samples,
a 600 s test timeout plus a 240 s webServer timeout). On PR #3732 at ac3ed20d (Mon 21 Sep
2026) it ran inside pytest shard 3 from 20:09:53Z until the shard's 1380 s wall budget killed
the whole shard at 34% progress. The pytest lanes install no frontend node_modules and no
browsers, so they are the wrong host whatever the duration.

[if] the bench is collected by a pytest lane, or run by no lane [then] broken, [else stop].

Regression lines:
  - if either pytest lane in ci.yml stops ignoring the bench file then broken
  - if the e2e extended job stops running the bench, or runs it without the
    ``!cancelled()`` guard every other extended suite carries, then broken
    (ignored everywhere and run nowhere would be a disabled test, which is banned)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.requirement("INFRA-03")

REPO = Path(__file__).resolve().parents[2]
CI = REPO / ".github" / "workflows" / "ci.yml"
E2E = REPO / ".github" / "workflows" / "e2e.yml"
BENCH = "tests/perf/test_library_playlist_switch_bench.py"


def _steps(workflow: Path, job: str) -> list[dict]:
    document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    return document["jobs"][job]["steps"]


def _pytest_runs(workflow: Path, job: str) -> list[str]:
    runs = [step.get("run") or "" for step in _steps(workflow, job)]
    return [run for run in runs if ".venv/bin/pytest" in run]


def test_both_pytest_lanes_ignore_the_bench() -> None:
    """[if] a pytest lane collects the bench [then] a shard or leg times out, [else stop]."""
    for job in ("test", "fast"):
        runs = _pytest_runs(CI, job)
        assert runs, f"no pytest invocation found in ci.yml job {job!r}"
        for run in runs:
            assert f"--ignore={BENCH}" in run, f"ci.yml job {job!r} does not ignore {BENCH}"


def test_the_bench_file_still_exists_and_is_collected_nowhere_else_in_ci() -> None:
    """[if] the bench file is renamed or gone [then] the ignore matches nothing, [else stop]."""
    assert (REPO / BENCH).is_file(), f"{BENCH} is gone; drop the ignore and this pin"


def test_the_e2e_extended_job_runs_the_bench_guarded() -> None:
    """[if] the extended job loses the bench step or guard [then] it runs nowhere, [else stop]"""
    steps = [s for s in _steps(E2E, "extended") if BENCH in (s.get("run") or "")]
    assert len(steps) == 1, f"expected exactly one extended step running {BENCH}, got {len(steps)}"
    step = steps[0]
    assert ".venv/bin/pytest" in step["run"], "the bench is a pytest test; run it through pytest"
    assert "!cancelled()" in str(step.get("if", "")), (
        "the bench step must carry the !cancelled() guard every extended suite carries"
    )
