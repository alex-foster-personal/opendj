"""The stranded-pool check for CI_RUNS_ON_MERGE_QUEUE (DEVOPS-18, scripts/ci_merge_queue_pool.py).

Runner fixtures copy the label sets the live runner listing reported on Tue 29 Sep 2026
(`gh api repos/<repo>/actions/runners`), plus the planned reserve shape: a host label and
`mq` only.

Regression lines:
  - if a set variable with zero online matching runners reads OK then batches stall silently
  - if offline-only matches read OK then a crashed reserve looks healthy
  - if an unset variable reads STRANDED then the off state pages someone for nothing
  - if label case decides the match then `linux` never matches a runner's `Linux`
  - if a partial runner listing yields a verdict then a missing page reads as no runner
  - if a malformed variable yields a verdict then UNKNOWN is rendered as a finding
  - if a reserve runner shared with CI_RUNS_ON_PYTEST is not flagged then the reserve is not one
"""

from __future__ import annotations

import pytest

from scripts.ci_merge_queue_pool import (
    MeasurementError,
    Runner,
    items_from_pages,
    parse_labels,
    pool_verdict,
)

MQ = '["self-hosted","linux","mq"]'
OTHER_POOLS = {
    "CI_RUNS_ON_PYTEST": '["self-hosted","linux","pytest"]',
    "CI_RUNS_ON_LINUX": '["self-hosted","linux","linux-light"]',
    "CI_RUNS_ON_E2E": '["self-hosted","linux","e2e"]',
}


def _runner(name: str, *labels: str, online: bool = True, busy: bool = False) -> Runner:
    defaults = ("self-hosted", "Linux", "X64")
    return Runner(name, online, busy, frozenset(x.lower() for x in (*defaults, *labels)))


RESERVE = [_runner(f"agbox2-{n}", "agbox2", "mq", busy=n == 9) for n in range(9, 13)]
GENERAL = [
    _runner("agbox2-3", "agentbox", "pytest", "trunk", "agbox2", "linux-light"),
    _runner("agbox2-1", "e2e", "agbox2"),
]


def test_reserve_online_is_ok_and_counts_busy() -> None:
    """if a healthy reserve does not read OK then the check cries wolf"""
    verdict = pool_verdict(MQ, GENERAL + RESERVE, OTHER_POOLS)
    assert (verdict.status, verdict.exit_code) == ("OK", 0)
    assert "4 online runner(s) match, 1 busy" in verdict.lines[0]
    assert not [line for line in verdict.lines if line.startswith("[WARN]")]


def test_no_matching_runner_is_stranded() -> None:
    """if a set variable with zero online matching runners reads OK then batches stall"""
    verdict = pool_verdict(MQ, GENERAL, OTHER_POOLS)
    assert (verdict.status, verdict.exit_code) == ("STRANDED", 1)
    assert "0 online runners" in verdict.lines[0]
    assert "gh variable delete CI_RUNS_ON_MERGE_QUEUE" in "\n".join(verdict.lines)


def test_offline_only_matches_are_still_stranded() -> None:
    """if offline-only matches read OK then a crashed reserve looks healthy"""
    offline = [_runner("agbox3-9", "agbox3", "mq", online=False)]
    verdict = pool_verdict(MQ, GENERAL + offline, OTHER_POOLS)
    assert verdict.status == "STRANDED"
    assert "agbox3-9" in verdict.lines[1]


def test_unset_variable_is_off_not_stranded() -> None:
    """if an unset variable reads STRANDED then the off state pages someone for nothing"""
    verdict = pool_verdict(None, GENERAL, OTHER_POOLS)
    assert (verdict.status, verdict.exit_code) == ("OFF", 0)


def test_label_case_does_not_decide_the_match() -> None:
    """if label case decides the match then `linux` never matches a runner's `Linux`"""
    assert parse_labels('["Self-Hosted","LINUX","MQ"]') == parse_labels(MQ)
    assert pool_verdict('["self-hosted","LINUX","mq"]', RESERVE, {}).status == "OK"


def test_a_single_string_label_is_accepted() -> None:
    """if a bare-string runs-on value is refused then a valid variable reads UNKNOWN"""
    assert parse_labels('"mq"') == frozenset({"mq"})


@pytest.mark.parametrize("value", ["not json", "[]", "[1, 2]", '{"labels": ["mq"]}'])
def test_malformed_variable_is_unknown(value: str) -> None:
    """if a malformed variable yields a verdict then UNKNOWN is rendered as a finding"""
    with pytest.raises(MeasurementError):
        pool_verdict(value, RESERVE, OTHER_POOLS)


def test_reserve_shared_with_another_pool_warns() -> None:
    """if a reserve runner also matched by CI_RUNS_ON_PYTEST is not flagged then it is shared"""
    shared = _runner("agbox3-10", "agbox3", "mq", "pytest")
    verdict = pool_verdict(MQ, [*RESERVE, shared], OTHER_POOLS)
    assert verdict.exit_code == 0
    warns = [line for line in verdict.lines if line.startswith("[WARN]")]
    assert warns == ["[WARN] reserve not exclusive: agbox3-10 also serves CI_RUNS_ON_PYTEST"]


def test_offline_reserve_runner_shared_with_another_pool_warns() -> None:
    """if an offline reserve runner that also serves CI_RUNS_ON_PYTEST reads clean then broken"""
    shared = _runner("agbox3-10", "agbox3", "mq", "pytest", online=False)
    verdict = pool_verdict(MQ, [*RESERVE, shared], OTHER_POOLS)
    assert (verdict.status, verdict.exit_code) == ("OK", 0)
    warns = [line for line in verdict.lines if line.startswith("[WARN]")]
    assert warns == ["[WARN] reserve not exclusive: agbox3-10 also serves CI_RUNS_ON_PYTEST"]


def test_partial_listing_is_unknown() -> None:
    """if a partial runner listing yields a verdict then a missing page reads as no runner"""
    short = [{"total_count": 3, "runners": [{"name": "a"}]}]
    with pytest.raises(MeasurementError, match="read 1 of total_count 3"):
        items_from_pages(short, "runners", "repos/x/y/actions/runners")
    with pytest.raises(MeasurementError, match="no pages"):
        items_from_pages([], "runners", "repos/x/y/actions/runners")
    two_pages = [
        {"total_count": 2, "runners": [{"name": "a"}]},
        {"total_count": 2, "runners": [{"name": "b"}]},
    ]
    assert [r["name"] for r in items_from_pages(two_pages, "runners", "p")] == ["a", "b"]
