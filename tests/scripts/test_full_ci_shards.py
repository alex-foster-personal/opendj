"""full-ci.yml runs the whole suite as pytest-split shards and gates coverage once.

Every scheduled full-ci run from Thu 29 Oct to Wed 11 Nov 2026 ended `cancelled`
on the job timeout: one serial job over ~37k tests reached 4 percent in 39 minutes.

  - [if] the matrix and the coverage job disagree on the shard count [then] the coverage combine misses shards, [else stop].
  - [if] a shard runs without --splits/--group [then] every shard reruns the whole suite, [else stop].
  - [if] a shard keeps --cov-fail-under [then] one slice reds on a partial number, [else stop].
  - [if] the coverage job loses the 60 percent gate [then] nothing gates coverage, [else stop].
  - [if] the shards share one coverage data file name [then] combine loses all but one, [else stop].
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
FULL_CI = REPO_ROOT / ".github" / "workflows" / "full-ci.yml"

pytestmark = pytest.mark.requirement("CIPAR-12894")


def _jobs() -> dict:
    return yaml.safe_load(FULL_CI.read_text(encoding="utf-8"))["jobs"]


def _pytest_step() -> dict:
    steps = _jobs()["test"]["steps"]
    return next(s for s in steps if ".venv/bin/pytest" in str(s.get("run", "")))


def test_matrix_and_coverage_job_agree_on_the_shard_count() -> None:
    """[if] the matrix and the coverage job disagree on the shard count [then] fail, [else stop]."""
    jobs = _jobs()
    shards = jobs["test"]["strategy"]["matrix"]["shard"]
    assert shards == list(range(1, len(shards) + 1)), shards
    assert len(shards) >= 2, "full-ci.yml is no longer sharded; one job cannot hold the suite"
    assert jobs["test"]["strategy"]["fail-fast"] is False
    assert int(jobs["coverage"]["env"]["FULL_CI_SHARDS"]) == len(shards)


def test_each_shard_runs_only_its_slice_into_its_own_data_file() -> None:
    """[if] a shard runs without --splits/--group or shares a data file name [then] fail, [else stop]."""
    step = _pytest_step()
    run = step["run"]
    assert "--splits ${{ strategy.job-total }} --group ${{ matrix.shard }}" in run, run
    assert "--collect-floor 3700" in run, "the full-suite floor must stay on every shard"
    assert "--cov-fail-under" not in run, "a single slice cannot meet the whole-suite gate"
    assert step["env"]["COVERAGE_FILE"] == ".coverage.shard-${{ matrix.shard }}"


def test_the_coverage_job_combines_every_shard_and_gates_at_60() -> None:
    """[if] the coverage job loses the combine or the 60 percent gate [then] fail, [else stop]."""
    job = _jobs()["coverage"]
    assert job["needs"] == "test"
    assert job["if"] == "${{ !cancelled() }}"
    script = "\n".join(str(s.get("run", "")) for s in job["steps"])
    assert "coverage-data-shard-$shard" in script
    assert "cov combine --keep cov-data" in script
    assert "cov report --fail-under=60" in script


def test_coverage_paths_map_every_runner_root_back_to_apps() -> None:
    """[if] [tool.coverage.paths] goes [then] shards from different runners do not merge, so fail, [else stop]."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["tool"]["coverage"]["paths"]["source"] == ["apps/", "*/apps/"]
