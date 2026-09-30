"""Trunk Merge Queue settings keep the combination Trunk's docs recommend (MQCFG-01).

ADR-NEW-trunk-queue-settings-audited-against-docs. `ci/trunk-merge-queue.json`
mirrors the live queue. These tests pin INVARIANTS between its settings, never a
single tuned value, so a later tuning round can move a number without editing a
test, but cannot break a combination the docs say only works together.

Regression lines:
  - if optimistic merging is on and pending failure depth is 0 then anti-flake
    protection is silently off and every flake evicts its PR
  - if pending failure depth drops below 2 then a flaky batch is evicted before a
    second successor can clear it, the eviction class measured Mon 28 to Wed 30 Sep
  - if bisection concurrency is below testing concurrency then a failed batch
    isolates its culprit slower than the queue admits new work
  - if batching is on without the bisection optimization then a failed batch
    fails every member instead of retesting the innocent ones
  - if direct merge is switched on then a PR can land on main without a queue test
  - if the test timeout is disabled then a draft whose CI never starts holds its
    PRs forever instead of failing after a bounded wait
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
QUEUE_CONFIG = REPO_ROOT / "ci" / "trunk-merge-queue.json"
# Measured eviction class (ADR): innocent PRs evicted at depth 1. Raising the floor
# is a new round; lowering it needs the ADR's measurement to say depth 1 suffices.
MIN_PENDING_FAILURE_DEPTH = 2


# -----------------------------------------------------------------------------
@pytest.fixture(scope="module")
def queue() -> dict[str, Any]:
    settings = json.loads(QUEUE_CONFIG.read_text())
    assert isinstance(settings, dict) and settings, f"{QUEUE_CONFIG} is not a settings object"
    return settings


def _setting(queue: dict[str, Any], key: str) -> Any:
    assert key in queue, f"{QUEUE_CONFIG.name} has no {key!r}; the invariant cannot be measured"
    return queue[key]


# -----------------------------------------------------------------------------
def test_anti_flake_protection_is_complete(queue: dict[str, Any]) -> None:
    """Optimistic merging only clears flakes when pending failure depth is above 0."""
    assert _setting(queue, "canOptimisticallyMerge") is True
    depth = _setting(queue, "pendingFailureDepth")
    assert type(depth) is int and depth >= MIN_PENDING_FAILURE_DEPTH, depth


def test_bisection_is_at_least_as_wide_as_testing(queue: dict[str, Any]) -> None:
    assert _setting(queue, "bisectionConcurrency") >= _setting(queue, "concurrency")


def test_batches_bisect_rather_than_fail_whole(queue: dict[str, Any]) -> None:
    batch = _setting(queue, "batch")
    assert type(batch) is bool, f"batch must be a boolean, got {batch!r}"
    if batch is True:
        assert _setting(queue, "optimizationMode") == "bisection_skip_redundant_tests"
    elif batch is False:
        pytest.skip("batching is off, so there is no batch to bisect")


def test_every_merge_is_queue_tested(queue: dict[str, Any]) -> None:
    assert _setting(queue, "directMergeMode") == "off"


def test_a_test_run_has_a_bounded_timeout(queue: dict[str, Any]) -> None:
    timeout = _setting(queue, "testingTimeoutMinutes")
    # type() not isinstance(): bool subclasses int, so JSON `true` must not pass as 1 minute.
    assert type(timeout) is int and timeout > 0, timeout


def test_a_known_bad_combination_is_rejected() -> None:
    """Negative control: the depth check must fire on the pre-audit value of 1."""
    with pytest.raises(AssertionError):
        test_anti_flake_protection_is_complete(
            {"canOptimisticallyMerge": True, "pendingFailureDepth": 1}
        )


@pytest.mark.parametrize("batch", [None, "true", 1])
def test_a_non_boolean_batch_setting_is_rejected(batch: Any) -> None:
    """Negative control: a malformed batch value must fail, not skip both branches."""
    with pytest.raises(AssertionError):
        test_batches_bisect_rather_than_fail_whole({"batch": batch})


@pytest.mark.parametrize("timeout", [True, 0, "300"])
def test_a_non_integer_or_unbounded_timeout_is_rejected(timeout: Any) -> None:
    """Negative control: JSON true is a bool, not a 1-minute timeout."""
    with pytest.raises(AssertionError):
        test_a_test_run_has_a_bounded_timeout({"testingTimeoutMinutes": timeout})
