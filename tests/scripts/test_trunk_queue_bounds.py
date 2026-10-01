"""Trunk Merge Queue waits stay bounded (MQCFG-02).

`ci/trunk-merge-queue.json` mirrors the live queue. These tests pin CEILINGS, never a
tuned value: a tuning round may move either number below its ceiling without editing
a test, but cannot make a stall or a lone PR's wait unbounded in practice.

Regression lines:
  - if the test timeout rises above 180 minutes then a batch stranded on an offline
    runner pool holds every PR behind it for over three hours before anything reads red
  - if the batching wait rises above 30 minutes then a lone PR, or a repair, sits idle
    for over half an hour before its draft even starts
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
QUEUE_CONFIG = REPO_ROOT / "ci" / "trunk-merge-queue.json"
# Slowest trial runs measured Thu 1 Oct 2026 (trunk-merge/* since Mon 28 Sep): ci.yml
# 45.8 min (n=44), e2e.yml 58.1 min (n=86). The ceiling keeps the timeout near 2x
# that, not the 5 h it was, which a stranded reserve pool waited out in full.
MAX_TESTING_TIMEOUT_MINUTES = 180
MAX_BATCHING_WAIT_MINUTES = 30


# -----------------------------------------------------------------------------
@pytest.fixture(scope="module")
def queue() -> dict[str, Any]:
    settings = json.loads(QUEUE_CONFIG.read_text())
    assert isinstance(settings, dict) and settings, f"{QUEUE_CONFIG} is not a settings object"
    return settings


def _minutes(queue: dict[str, Any], key: str) -> int:
    assert key in queue, f"{QUEUE_CONFIG.name} has no {key!r}; the bound cannot be measured"
    value = queue[key]
    # type() not isinstance(): bool subclasses int, so JSON `true` must not pass as 1 minute.
    assert type(value) is int and value > 0, f"{key} must be a positive integer, got {value!r}"
    return value


# -----------------------------------------------------------------------------
def test_a_stranded_batch_fails_within_three_hours(queue: dict[str, Any]) -> None:
    timeout = _minutes(queue, "testingTimeoutMinutes")
    assert timeout <= MAX_TESTING_TIMEOUT_MINUTES, timeout


def test_a_lone_pr_waits_at_most_half_an_hour_for_a_batch(queue: dict[str, Any]) -> None:
    wait = _minutes(queue, "batchingMaxWaitTimeMinutes")
    assert wait <= MAX_BATCHING_WAIT_MINUTES, wait


# -----------------------------------------------------------------------------
@pytest.mark.parametrize("timeout", [300, 181, True, 0, "120"])
def test_an_unbounded_or_malformed_timeout_is_rejected(timeout: Any) -> None:
    """Negative control: the pre-round value of 300 and malformed values must fail."""
    with pytest.raises(AssertionError):
        test_a_stranded_batch_fails_within_three_hours({"testingTimeoutMinutes": timeout})


@pytest.mark.parametrize("wait", [31, 60, True, 0, "20"])
def test_an_unbounded_or_malformed_batching_wait_is_rejected(wait: Any) -> None:
    """Negative control: a wait past the ceiling and malformed values must fail."""
    with pytest.raises(AssertionError):
        test_a_lone_pr_waits_at_most_half_an_hour_for_a_batch({"batchingMaxWaitTimeMinutes": wait})


@pytest.mark.parametrize(("timeout", "wait"), [(120, 20), (180, 30), (1, 1)])
def test_values_at_or_below_the_ceilings_pass(timeout: int, wait: int) -> None:
    """Positive control: the bounds must not reject the values this round sets."""
    test_a_stranded_batch_fails_within_three_hours({"testingTimeoutMinutes": timeout})
    test_a_lone_pr_waits_at_most_half_an_hour_for_a_batch({"batchingMaxWaitTimeMinutes": wait})
