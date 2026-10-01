"""Bookkeeping workflows stay on the self-hosted agentbox pool.

Regression lines:
  - if CI Cost Guard moves off CI_RUNS_ON_LINUX then broken
  - if Stable evidence moves off CI_RUNS_ON_LINUX then broken
  - if bookkeeping cancel-in-progress flips to true then broken
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = {
    "CI Cost Guard": REPO / ".github" / "workflows" / "ci-cost-guard.yml",
    "Stable evidence": REPO / ".github" / "workflows" / "stable-evidence.yml",
}
RUNS_ON_EXPR = '${{ fromJSON(vars.CI_RUNS_ON_LINUX || \'"ubuntu-latest"\') }}'


def _workflow(path: Path) -> dict:
    assert path.is_file(), f"workflow is missing: {path}"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path.name} is not a mapping"
    return document


def test_bookkeeping_workflows_use_agentbox_runner_pool() -> None:
    """Each bookkeeping workflow still resolves runs-on through CI_RUNS_ON_LINUX."""
    for name, path in WORKFLOWS.items():
        workflow = _workflow(path)
        job = next(iter(workflow["jobs"].values()))
        assert job["runs-on"] == RUNS_ON_EXPR, f"{name} must stay on CI_RUNS_ON_LINUX"


def test_bookkeeping_workflows_keep_cancel_in_progress_false() -> None:
    """Bookkeeping concurrency must not cancel in-flight append/price/post work."""
    for name, path in WORKFLOWS.items():
        workflow = _workflow(path)
        concurrency = workflow.get("concurrency") or {}
        assert concurrency.get("cancel-in-progress") is False, (
            f"{name} must keep cancel-in-progress: false"
        )
