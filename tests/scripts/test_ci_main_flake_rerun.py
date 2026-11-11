"""Main flake rerun decision (#11377).

Regression lines:
  - if one failed pytest shard on attempt 1 is not rerun then broken
  - if attempt 2 is rerun again then broken
  - if a non-shard failure or two failed shards are rerun then broken

-Claude
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

SHARD = "pytest fast lane (shard 3 of 5)"
WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci-main-flake-rerun.yml"


def _document() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def should_rerun(attempt: int, failed: list[str]) -> bool:
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq is not installed")
    run = {"attempt": attempt, "jobs": [{"name": name, "conclusion": "failure"} for name in failed]}
    run["jobs"].append({"name": "pytest fast lane (shard 2 of 5)", "conclusion": "success"})
    filt = _document()["jobs"]["rerun"]["env"]["DECIDE_JQ"]
    out = subprocess.run([jq, "-r", filt], input=json.dumps(run), capture_output=True, text=True, check=True)
    verdict = out.stdout.strip()
    assert verdict in {"rerun", "leave"}, verdict
    return verdict == "rerun"


def test_one_failed_shard_on_attempt_one_is_rerun() -> None:
    assert should_rerun(1, [SHARD, "ci gate"])


def test_a_second_attempt_is_never_rerun() -> None:
    assert not should_rerun(2, [SHARD, "ci gate"])


def test_two_failed_shards_are_left_red() -> None:
    assert not should_rerun(1, [SHARD, "pytest fast lane (shard 1 of 5)", "ci gate"])


def test_a_non_shard_failure_is_left_red() -> None:
    assert not should_rerun(1, [SHARD, "frontend unit + check + build", "ci gate"])


def test_the_workflow_runs_only_on_first_attempt_main_push_failures() -> None:
    document = _document()
    trigger = document[True]["workflow_run"]
    assert trigger["workflows"] == ["CI"] and trigger["branches"] == ["main"]
    condition = document["jobs"]["rerun"]["if"]
    for clause in ("conclusion == 'failure'", "event == 'push'", "run_attempt == 1"):
        assert clause in condition
