"""Trunk Merge Queue waits stay bounded (MQCFG-02, round 2).

`ci/trunk-merge-queue.json` mirrors the live queue. These tests pin CEILINGS, never a
tuned value: a tuning round may move either number below its ceiling without editing
a test, but cannot make a stall or a lone PR's wait unbounded in practice.

Regression lines:
  - if the test timeout rises above 180 minutes then a batch stranded on an offline
    runner pool holds every PR behind it for over three hours before anything reads red
  - if the batching wait rises above 5 minutes then a lone PR, or a repair, sits idle
    before its draft even starts (round 9 hunt: at 20 minutes the median PR idled 21.6 of
    its 36.6 queue minutes, and 26 of 59 batches still tested one PR)
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
MAX_BATCHING_WAIT_MINUTES = 5


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


def test_a_lone_pr_waits_at_most_five_minutes_for_a_batch(queue: dict[str, Any]) -> None:
    wait = _minutes(queue, "batchingMaxWaitTimeMinutes")
    assert wait <= MAX_BATCHING_WAIT_MINUTES, wait


# -----------------------------------------------------------------------------
@pytest.mark.parametrize("timeout", [300, 181, True, 0, "120"])
def test_an_unbounded_or_malformed_timeout_is_rejected(timeout: Any) -> None:
    """Negative control: the pre-round value of 300 and malformed values must fail."""
    with pytest.raises(AssertionError):
        test_a_stranded_batch_fails_within_three_hours({"testingTimeoutMinutes": timeout})


@pytest.mark.parametrize("wait", [20, 6, 31, True, 0, "1"])
def test_an_unbounded_or_malformed_batching_wait_is_rejected(wait: Any) -> None:
    """Negative control: a wait past the ceiling and malformed values must fail."""
    with pytest.raises(AssertionError):
        test_a_lone_pr_waits_at_most_five_minutes_for_a_batch({"batchingMaxWaitTimeMinutes": wait})


@pytest.mark.parametrize(("timeout", "wait"), [(120, 1), (180, 5), (1, 1)])
def test_values_at_or_below_the_ceilings_pass(timeout: int, wait: int) -> None:
    """Positive control: the bounds must not reject the values this round sets."""
    test_a_stranded_batch_fails_within_three_hours({"testingTimeoutMinutes": timeout})
    test_a_lone_pr_waits_at_most_five_minutes_for_a_batch({"batchingMaxWaitTimeMinutes": wait})
