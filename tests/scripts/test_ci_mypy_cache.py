"""CI must persist mypy's incremental cache outside the workspace (#1496).

Without this step every quality job and shard-4 `test_quality_gate.py` run
pays for a cold mypy scan even when the tree is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CACHE_STEP = "Keep the mypy incremental cache across jobs on this runner"


def _quality_job() -> dict:
    doc = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    return doc["jobs"]["quality"]


def test_quality_job_persists_mypy_cache_before_the_ratchet() -> None:
    steps = _quality_job()["steps"]
    names = [step.get("name") for step in steps]
    cache_idx = names.index(CACHE_STEP)
    ratchet_idx = names.index("Quality ratchet")
    assert cache_idx < ratchet_idx, (
        "if the mypy cache step runs after the ratchet then the first mypy "
        "invocation in the job is still cold"
    )
    run = steps[cache_idx].get("run") or ""
    assert "scripts.mypy_cache import config_identity" in run
    assert "MDT_MYPY_CACHE_DIR" in run
    assert "${RUNNER_NAME" in run


def test_quality_ratchet_step_does_not_inline_a_workspace_cache_dir() -> None:
    run = next(
        step["run"]
        for step in _quality_job()["steps"]
        if step.get("name") == "Quality ratchet"
    )
    assert "--cache-dir" not in run, (
        "the ratchet must take its cache dir from scripts.mypy_cache via "
        "MDT_MYPY_CACHE_DIR, not a workspace path checkout deletes"
    )
