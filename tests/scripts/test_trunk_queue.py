"""Trunk Merge Queue settings as code (`ci/trunk-merge-queue.json`, `scripts/trunk_queue.py`).

Regression lines:
  - if settings_drift misses a changed value then a UI edit to the live queue goes unreported
  - if settings_drift treats a reordered bot list as drift then every diff run is red for nothing
  - if the config file may carry `state` then a config push resumes a queue paused for an incident
  - if a required status names no pull_request job then the queue waits on a check that never comes
  - if a required status is path-filtered or skippable then a docs-only draft times out in the queue
  - if requiredStatuses drops an aggregator then the queue merges without that workflow's verdict
  - if directMergeMode is on then a PR can merge untested by the queue while e2e skips PR heads
  - if the merge method drifts from merge_commit then queue merges differ from the step-7 lane merge
  - if Trunk's status check is enabled while ci_wait does not know its app then every waiter raises
  - if a missing token reaches the network then an unauthenticated call reads like a result
  - if an unreachable API exits 1 then an outage reads as drift
  - if an `OK`, empty or non-JSON read parses as data then an upstream fault reads as drift
  - if a manual dispatch can name a branch ref then branch code runs with the org token
  - if the config workflow gains a PR trigger then branch code runs with the org token in scope
  - if the scheduled run applies instead of diffing then a deliberate UI change is silently reverted
"""

from __future__ import annotations

import ast
import itertools
import json
import os
import re
import subprocess
import sys
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
    for path in _workflow_files():
        workflow = yaml.safe_load(path.read_text())
        triggers = workflow.get(True) or workflow.get("on") or {}
        if "pull_request" not in triggers:
            continue
        for job_id, job in workflow["jobs"].items():
            names.update(_expanded_job_names(job_id, job))
    return names


def _every_pull_request_check_names(workflows: Path, target_branch: str) -> set[str]:
    """Check-run names that RUN (not skip) on every pull request to `target_branch`, docs-only too.

    A queue draft whose PRs are all docs-only must still produce every required status, or
    Trunk waits out `testingTimeoutMinutes` and fails it (#3976, #4118, #4194). So a name counts
    only when its workflow's pull_request trigger carries no path filter and admits the target
    branch, and the job's `if` is PROVEN true on a pull_request event. An `if` this cannot
    evaluate is unproven, never assumed true.
    """
    names: set[str] = set()
    for path in sorted(workflows.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        triggers = workflow.get(True) or workflow.get("on") or {}
        if "pull_request" not in triggers:
            continue
        pr_filter = (triggers.get("pull_request") if isinstance(triggers, dict) else None) or {}
        if "paths" in pr_filter or "paths-ignore" in pr_filter:
            continue
        if target_branch not in pr_filter.get("branches", [target_branch]):
            continue
        for job_id, job in workflow["jobs"].items():
            if _job_runs_on_every_pull_request(job):
                names.update(_expanded_job_names(job_id, job))
    return names


_IF_TOKEN = re.compile(
    r"\s+|always\(\)|github\.event_name|inputs\.[A-Za-z0-9_-]+|'[^']*'|==|!=|&&|\|\||!(?=\s*\()|[()]"
)
_IF_TRANSLATION = {"&&": " and ", "||": " or ", "!": " not ", "always()": "True"}


def _job_runs_on_every_pull_request(job: dict[str, Any]) -> bool:
    """True only when the job's `if` evaluates true for pull_request with no workflow inputs."""
    condition = str(job.get("if", "")).strip()
    if condition.startswith("${{") and condition.endswith("}}"):
        condition = condition[3:-2].strip()
    if job.get("needs") and "always()" not in condition:
        return False  # a skipped or failed dependency skips the job, so its check never passes
    if not condition:
        return True
    tokens = _IF_TOKEN.findall(condition)
    if "".join(tokens) != condition:
        return False  # references a context this cannot evaluate: unproven
    python = "".join(
        _IF_TRANSLATION.get(tok)
        or ("'pull_request'" if tok == "github.event_name" else None)
        or ("''" if tok.startswith("inputs.") else tok)
        for tok in tokens
    )
    try:
        return _evaluate_condition(ast.parse(python, mode="eval").body) is True
    except (SyntaxError, ValueError):
        return False  # not an expression this can evaluate: unproven, so the job does not count


def _evaluate_condition(node: ast.expr) -> object:
    """Walk the translated `if`: boolean ops, `not`, `==`/`!=` and constants, nothing else."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _evaluate_condition(node.operand)
    if isinstance(node, ast.BoolOp):
        values = [_evaluate_condition(value) for value in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left, right = _evaluate_condition(node.left), _evaluate_condition(node.comparators[0])
        if isinstance(node.ops[0], ast.Eq):
            return left == right
        if isinstance(node.ops[0], ast.NotEq):
            return left != right
    raise ValueError(f"unsupported workflow `if` construct: {ast.dump(node)}")


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


def _run_cli(*args: str, token: str | None) -> subprocess.CompletedProcess[str]:
    """The real CLI and the real transport, pointed at a proxy port nothing listens on.

    No network access is patched out: urllib honors HTTPS_PROXY, so any request that is actually
    attempted fails as a refused connection, which the CLI must report as unreachable.
    """
    env = {
        k: v for k, v in os.environ.items() if k not in {"TRUNK_API_TOKEN", "NO_PROXY", "no_proxy"}
    }
    env["HTTPS_PROXY"] = env["https_proxy"] = "http://127.0.0.1:9"
    if token is not None:
        env["TRUNK_API_TOKEN"] = token
    return subprocess.run(
        [sys.executable, "-m", "scripts.trunk_queue", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_missing_token_never_reaches_the_network() -> None:
    result = _run_cli("diff", token=None)
    assert result.returncode == 2, "a failed read must not exit like a clean diff (0) or drift (1)"
    assert "TRUNK_API_TOKEN is not set" in result.stderr
    assert "unreachable" not in result.stderr, "the token check must come before any request"


def test_an_unreachable_api_is_unmeasured_not_drift() -> None:
    """Control for the test above: with a token, the same CLI does attempt the request."""
    result = _run_cli("diff", token="not-a-real-token")
    assert result.returncode == 2, result.stderr
    assert "Trunk API unreachable" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("body", "parsed"),
    [(b"OK", {}), (b"OK\n", {}), (b"", {}), (b'{"state": "running"}', {"state": "running"})],
)
def test_a_plain_text_success_body_is_not_an_error(body: bytes, parsed: dict[str, str]) -> None:
    """Trunk answers write endpoints with a bare `OK`; only JSON bodies carry data."""
    assert trunk_queue.parse_body(body, endpoint="updateQueue") == parsed


@pytest.mark.parametrize("endpoint", ["getQueue", "updateQueue"])
def test_an_unexpected_non_json_body_is_unmeasured_not_a_traceback(endpoint: str) -> None:
    with pytest.raises(trunk_queue.TrunkApiError, match="not JSON"):
        trunk_queue.parse_body(b"<html>", endpoint=endpoint)


@pytest.mark.parametrize("body", [b"OK", b"", b"[]", b'"running"'])
def test_a_read_without_a_json_object_is_unmeasured_not_drift(body: bytes) -> None:
    """An `OK` or empty read would otherwise diff as every setting drifted (exit 1, not 2)."""
    with pytest.raises(trunk_queue.TrunkApiError, match="getQueue"):
        trunk_queue.parse_body(body, endpoint="getQueue")


@pytest.mark.parametrize("endpoint", ["getQueue", "updateQueue"])
def test_an_undecodable_success_body_is_unmeasured_not_a_traceback(endpoint: str) -> None:
    """Invalid bytes raise UnicodeDecodeError, not JSONDecodeError: exit 2, never drift's exit 1."""
    with pytest.raises(trunk_queue.TrunkApiError, match="not JSON"):
        trunk_queue.parse_body(b"\xff\xfe{", endpoint=endpoint)


def test_a_read_with_a_json_object_is_data() -> None:
    """Control for the test above: a real queue read still parses."""
    parsed = trunk_queue.parse_body(b'{"state": "running"}', endpoint="getQueue")
    assert parsed == {"state": "running"}


# -----------------------------------------------------------------------------
def test_every_required_status_is_a_pull_request_job() -> None:
    produced = _pull_request_check_names()
    assert "pytest fast lane (shard 1 of 5)" in produced, "positive control: matrix expansion"
    missing = sorted(set(DESIRED["requiredStatuses"]) - produced)
    assert DESIRED["requiredStatuses"], "an empty list tells Trunk to require nothing"
    assert not missing, f"required statuses no pull_request job produces: {missing}"


AGGREGATORS = {"ci gate", "e2e verdict"}


def test_the_queue_requires_exactly_the_always_created_aggregators() -> None:
    """Per-job names are path-filtered or scope-skipped on docs-only PRs; the aggregators are not.

    Exact, not a subset: dropping one merges without that workflow's verdict, which the
    every-pull-request guard below cannot see because a smaller list still passes it.
    """
    assert set(DESIRED["requiredStatuses"]) == AGGREGATORS
    assert len(DESIRED["requiredStatuses"]) == len(AGGREGATORS), "duplicates hide a missing name"


def test_every_required_status_runs_on_every_pull_request_docs_only_included() -> None:
    """Needs PR #4221's `ci gate` and `e2e verdict` on main; red before that merges, by design."""
    always = _every_pull_request_check_names(WORKFLOWS, DESIRED["targetBranch"])
    assert always, "no job proven to run on every pull request: the evaluator is broken"
    not_always = sorted(set(DESIRED["requiredStatuses"]) - always)
    assert not not_always, (
        f"required statuses not created and run on a docs-only pull request: {not_always}. "
        "Trunk waits testingTimeoutMinutes for them, then fails the draft."
    )


@pytest.mark.parametrize(
    ("job", "runs"),
    [
        ({}, True),
        ({"if": "${{ always() }}", "needs": ["scope", "gate"]}, True),
        (
            {
                "if": "${{ always() && !(github.event_name == 'workflow_dispatch' "
                "&& inputs.tier == 'fast') }}",
                "needs": ["scope", "test"],
            },
            True,
        ),
        ({"needs": "scope"}, False),
        ({"if": "needs.scope.outputs.in_scope == 'true'", "needs": "scope"}, False),
        ({"if": "github.event_name == 'push'"}, False),
        ({"if": "always() && vars.CI_RUNS_ON_LINUX != ''", "needs": "scope"}, False),
        ({"if": "always() &&"}, False),  # every token known, but not an expression: SyntaxError
        ({"if": "()"}, False),  # parses, but as a construct the walker rejects: ValueError
    ],
)
def test_the_every_pull_request_evaluator(job: dict[str, Any], runs: bool) -> None:
    """Both directions: proven-true runs, and scope-gated, push-only or unparseable does not."""
    assert _job_runs_on_every_pull_request(job) is runs


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


# -----------------------------------------------------------------------------
CONFIG_WORKFLOW = WORKFLOWS / "trunk-queue-config.yml"


def test_the_config_workflow_never_runs_branch_code_with_the_token() -> None:
    workflow = yaml.safe_load(CONFIG_WORKFLOW.read_text())
    triggers = set(workflow.get(True) or workflow["on"])
    assert triggers == {"push", "schedule", "workflow_dispatch"}, triggers
    assert (workflow.get(True) or workflow["on"])["push"]["branches"] == ["main"]


def _workflow_files() -> list[Path]:
    """Every workflow GitHub runs: both extensions, so a `.yaml` file cannot hide from a scan."""
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def _trunk_token_holders() -> list[str]:
    """Every job, or workflow-level block, whose definition mentions the Trunk token at all."""
    holders: list[str] = []
    for path in _workflow_files():
        workflow = yaml.safe_load(path.read_text())
        outside_jobs = {key: value for key, value in workflow.items() if key != "jobs"}
        if "TRUNK_API_TOKEN" in json.dumps(outside_jobs, default=str):
            holders.append(f"{path.name}:<workflow>")
        holders += [
            f"{path.name}:{job_id}"
            for job_id, job in workflow["jobs"].items()
            if "TRUNK_API_TOKEN" in json.dumps(job, default=str)
        ]
    return holders


def test_only_the_config_sync_job_references_the_trunk_token() -> None:
    """With or without a checkout: branch-controlled commands must never see the token."""
    assert _trunk_token_holders() == [f"{CONFIG_WORKFLOW.name}:sync"]


def test_only_mains_reviewed_code_can_hold_the_token() -> None:
    """`gh workflow run --ref <branch>` would otherwise run branch code with the org token."""
    job = yaml.safe_load(CONFIG_WORKFLOW.read_text())["jobs"]["sync"]
    assert job["if"] == "github.ref == 'refs/heads/main'"


def test_the_scheduled_run_only_diffs() -> None:
    step = yaml.safe_load(CONFIG_WORKFLOW.read_text())["jobs"]["sync"]["steps"][-1]
    assert step["env"]["MODE"] == "${{ github.event_name == 'schedule' && 'diff' || 'apply' }}"
    assert step["run"] == 'python3 -m scripts.trunk_queue "$MODE"'
