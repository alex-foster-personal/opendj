"""Trunk Merge Queue settings as code (`ci/trunk-merge-queue.json`, `scripts/trunk_queue.py`).

Regression lines:
  - if settings_drift misses a changed value then a UI edit to the live queue goes unreported
  - if settings_drift treats a reordered bot list as drift then every diff run is red for nothing
  - if the config file may carry `state` then a config push resumes a queue paused for an incident
  - if a required status names no pull_request job then the queue waits on a check that never comes
  - if directMergeMode is on then a PR can merge untested by the queue while e2e skips PR heads
  - if the merge method drifts from merge_commit then queue merges differ from the step-7 lane merge
  - if Trunk's status check is enabled while ci_wait does not know its app then every waiter raises
  - if a missing token reaches the network then an unauthenticated call reads like a result
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import trunk_queue
from scripts.ci_wait_core import DROPPED_APP_CHECKS

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DESIRED = trunk_queue.load_desired()


# -----------------------------------------------------------------------------
def _pull_request_check_names() -> set[str]:
    """Check-run names of every job in a workflow with a pull_request trigger, matrix-expanded."""
    names: set[str] = set()
    for path in WORKFLOWS.glob("*.yml"):
        workflow = yaml.safe_load(path.read_text())
        triggers = workflow.get(True) or workflow.get("on") or {}
        if "pull_request" not in triggers:
            continue
        for job_id, job in workflow["jobs"].items():
            names.update(_expanded_job_names(job_id, job))
    return names


def _expanded_job_names(job_id: str, job: dict[str, Any]) -> set[str]:
    name = str(job.get("name", job_id))
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    axes = {key: values for key, values in matrix.items() if isinstance(values, list)}
    if not axes:
        return {name}
    expanded = set()
    for combo in itertools.product(*axes.values()):
        rendered = name
        for key, value in zip(axes, combo, strict=True):
            rendered = re.sub(r"\$\{\{\s*matrix\." + key + r"\s*\}\}", str(value), rendered)
        expanded.add(rendered)
    return expanded


# -----------------------------------------------------------------------------
def test_drift_reports_a_changed_value() -> None:
    assert trunk_queue.settings_drift({"concurrency": 3}, {"concurrency": 5}) == {
        "concurrency": (3, 5)
    }


def test_drift_reports_a_setting_the_live_queue_lacks() -> None:
    assert "batch" in trunk_queue.settings_drift({"batch": True}, {})


def test_drift_ignores_order_of_unordered_lists_but_not_their_contents() -> None:
    desired = {"allowedBotSubmitters": ["a[bot]", "b[bot]"]}
    assert trunk_queue.settings_drift(desired, {"allowedBotSubmitters": ["b[bot]", "a[bot]"]}) == {}
    assert trunk_queue.settings_drift(desired, {"allowedBotSubmitters": ["a[bot]"]})


def test_drift_ignores_the_queue_address_and_live_only_fields() -> None:
    desired = {"repo": {"name": "x"}, "targetBranch": "main", "mode": "single"}
    live = {"mode": "single", "state": "paused", "enqueuedPullRequests": [1]}
    assert trunk_queue.settings_drift(desired, live) == {}


def test_the_config_cannot_carry_the_queue_run_state(tmp_path: Path) -> None:
    config = tmp_path / "queue.json"
    config.write_text(json.dumps({"state": "running"}))
    with pytest.raises(ValueError, match="operational"):
        trunk_queue.load_desired(config)
    assert "state" not in DESIRED


def test_a_missing_token_never_reaches_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.delenv("TRUNK_API_TOKEN", raising=False)
    monkeypatch.setattr(trunk_queue.urllib.request, "urlopen", lambda *a, **k: calls.append(a))
    with pytest.raises(trunk_queue.TrunkApiError, match="TRUNK_API_TOKEN"):
        trunk_queue.fetch_live(DESIRED)
    assert calls == []
    assert trunk_queue.main(["diff"]) == 2, "a failed read must not exit like a clean diff"


# -----------------------------------------------------------------------------
def test_every_required_status_is_a_pull_request_job() -> None:
    produced = _pull_request_check_names()
    assert "pytest fast lane (shard 1 of 5)" in produced, "positive control: matrix expansion"
    missing = sorted(set(DESIRED["requiredStatuses"]) - produced)
    assert DESIRED["requiredStatuses"], "an empty list tells Trunk to require nothing"
    assert not missing, f"required statuses no pull_request job produces: {missing}"


def test_the_queue_tests_every_pr_before_it_merges() -> None:
    assert DESIRED["directMergeMode"] == "off"
    assert DESIRED["createPrsForTestingBranches"] is True, "draft mode reuses pull_request CI"


def test_queue_merges_use_the_lane_merge_method() -> None:
    assert DESIRED["mergeMethod"] == "merge_commit"


def test_trunk_status_check_is_known_to_ci_wait_before_it_is_enabled() -> None:
    pair = ("trunk-io", f"Trunk Merge Queue ({DESIRED['targetBranch']})")
    if DESIRED["statusCheckEnabled"]:
        assert pair in DROPPED_APP_CHECKS, f"ci_wait_core raises on {pair} check-runs"


def test_the_nucbox_shim_label_submits_to_the_queue() -> None:
    assert DESIRED["labelCommandsEnabled"] is True
    assert DESIRED["enqueueingLabel"] == "queue"
