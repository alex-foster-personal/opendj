"""The outcome ledger behind terminal states and drain backoff (HEALTH-04, HEALTH-05).

Regression lines:
  - if an unchanged failure can be retried before its backoff elapses then broken
  - if a failure is still retried after MAX_ATTEMPTS then broken
  - if a changed audio file does not re-arm a failed track then broken
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.webui.server import coverage_outcomes as co

pytestmark = pytest.mark.requirement("HEALTH-05")


@pytest.fixture
def store(tmp_path: Path) -> co.OutcomeStore:
    return co.OutcomeStore(tmp_path / "state" / "coverage-outcomes.json")


def test_an_absent_ledger_is_empty_not_an_error(store: co.OutcomeStore) -> None:
    assert store.load() == {}


def test_failures_back_off_exponentially_then_go_terminal(store: co.OutcomeStore) -> None:
    first = store.record_failure("lyrics", "a", "sig", "boom", now=1000.0)
    assert first.attempts == 1
    assert co.may_attempt(first, "sig", now=1000.0 + co.BACKOFF_BASE_S - 1) is False
    assert co.may_attempt(first, "sig", now=1000.0 + co.BACKOFF_BASE_S) is True

    second = store.record_failure("lyrics", "a", "sig", "boom", now=2000.0)
    assert co.may_attempt(second, "sig", now=2000.0 + co.BACKOFF_BASE_S * 2 - 1) is False

    third = store.record_failure("lyrics", "a", "sig", "boom", now=3000.0)
    assert third.attempts == co.MAX_ATTEMPTS
    assert co.is_failed_terminal(third, "sig") is True
    assert co.may_attempt(third, "sig", now=10**12) is False   # never again, unchanged


def test_a_changed_signature_re_arms_a_terminal_failure(store: co.OutcomeStore) -> None:
    for moment in (1.0, 2.0, 3.0):
        outcome = store.record_failure("vocals", "a", "old", "boom", now=moment)
    assert co.may_attempt(outcome, "new", now=4.0) is True
    assert co.is_failed_terminal(outcome, "new") is False
    assert store.record_failure("vocals", "a", "new", "boom", now=5.0).attempts == 1


def test_the_ledger_survives_a_reload(store: co.OutcomeStore) -> None:
    store.record_no_source("stems", "a", "sig", "farm rejected it", now=1.0)
    reloaded = co.OutcomeStore(store.path).load()
    assert reloaded[("stems", "a")].kind == "no_source"
    assert reloaded[("stems", "a")].reason == "farm rejected it"


def test_clear_failures_leaves_no_source_alone(store: co.OutcomeStore) -> None:
    store.record_failure("lyrics", "a", "sig", "boom", now=1.0)
    store.record_no_source("stems", "b", "sig", "none", now=1.0)
    assert store.clear_failures() == 1
    assert set(store.load()) == {("stems", "b")}


def test_an_unknown_step_is_refused(store: co.OutcomeStore) -> None:
    with pytest.raises(ValueError, match="unknown step"):
        store.record_failure("frobnicate", "a", "sig", "boom", now=1.0)
