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
  - if a reserve runner on page 2 of the listing is dropped then a live pool reads STRANDED
  - if a listing stops asking for --paginate --slurp then gh output stops parsing
  - if a malformed variable yields a verdict then UNKNOWN is rendered as a finding
  - if a reserve runner shared with CI_RUNS_ON_PYTEST is not flagged then the reserve is not one
"""

from __future__ import annotations

import json
import subprocess

import pytest

from scripts import ci_merge_queue_pool
from scripts.ci_merge_queue_pool import (
    MeasurementError,
    Runner,
    items_from_pages,
    measure,
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


def test_another_reserve_variable_is_named_in_every_verdict() -> None:
    """if --variable CI_RUNS_ON_E2E_TRUNK (#5831) reports as the merge-queue pool then the
    operator preflights the wrong reserve"""
    e2e_trunk = '["self-hosted","linux","e2e-trunk"]'
    reserve = [_runner("agbox3-2", "agbox3", "e2e-trunk")]
    assert (
        pool_verdict(e2e_trunk, reserve, OTHER_POOLS, "CI_RUNS_ON_E2E_TRUNK")
        .lines[0]
        .startswith(f"[OK] CI_RUNS_ON_E2E_TRUNK={e2e_trunk}: 1 online runner(s)")
    )
    stranded = pool_verdict(e2e_trunk, GENERAL, OTHER_POOLS, "CI_RUNS_ON_E2E_TRUNK")
    assert stranded.status == "STRANDED"
    assert "gh variable delete CI_RUNS_ON_E2E_TRUNK" in "\n".join(stranded.lines)
    off = pool_verdict(None, reserve, OTHER_POOLS, "CI_RUNS_ON_E2E_TRUNK")
    assert off.status == "OFF" and "CI_RUNS_ON_E2E_TRUNK is unset" in off.lines[0]
    with pytest.raises(MeasurementError, match="CI_RUNS_ON_E2E_TRUNK is not JSON"):
        parse_labels("e2e-trunk", "CI_RUNS_ON_E2E_TRUNK")


def test_a_reserve_that_still_carries_e2e_warns() -> None:
    """if an `e2e-trunk` runner that kept `e2e` is not flagged then PR jobs can take the seat"""
    shared = [_runner("agbox3-2", "agbox3", "e2e", "e2e-trunk")]
    lines = pool_verdict(
        '["self-hosted","linux","e2e-trunk"]', shared, OTHER_POOLS, "CI_RUNS_ON_E2E_TRUNK"
    ).lines
    assert "[WARN] reserve not exclusive: agbox3-2 also serves CI_RUNS_ON_E2E" in lines


def _listed_runner(name: str, *labels: str) -> dict:
    return {
        "name": name,
        "status": "online",
        "busy": False,
        "labels": [{"name": label} for label in ("self-hosted", "Linux", *labels)],
    }


def _fake_gh(pages_by_endpoint: dict[str, list[dict]]):
    """gh's three output shapes: one page, one object per page, or slurped pages."""

    def run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        endpoint = next(arg for arg in args if arg.startswith("repos/"))
        pages = next(p for key, p in pages_by_endpoint.items() if key in endpoint)
        if "--paginate" in args and "--slurp" in args:
            stdout = json.dumps(pages)
        elif "--paginate" in args:
            stdout = "".join(json.dumps(page) for page in pages)
        else:
            stdout = json.dumps(pages[0])
        return subprocess.CompletedProcess(args, 0, stdout, "")

    return run


TWO_PAGES_OF_RUNNERS = {
    "actions/variables": [{"total_count": 1, "variables": [{"name": "CI_RUNS_ON_MERGE_QUEUE", "value": MQ}]}],
    "actions/runners": [
        {
            "total_count": 101,
            "runners": [_listed_runner(f"general-{n}", "pytest") for n in range(100)],
        },
        {"total_count": 101, "runners": [_listed_runner("agbox3-1", "agbox3", "mq")]},
    ],
}


def test_reserve_runner_on_page_two_is_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    """if a reserve runner on page 2 of the listing is dropped then a live pool reads STRANDED"""
    monkeypatch.setattr(ci_merge_queue_pool.subprocess, "run", _fake_gh(TWO_PAGES_OF_RUNNERS))
    verdict = measure("o/r", None)
    assert (verdict.status, verdict.exit_code) == ("OK", 0)
    assert "agbox3-1" in verdict.lines[0]


def test_concatenated_pages_are_unknown_not_a_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """if a listing stops asking for --paginate --slurp then gh output stops parsing"""
    fake = _fake_gh(TWO_PAGES_OF_RUNNERS)

    def without_slurp(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        runners_read = any("actions/runners" in arg for arg in args)
        return fake([arg for arg in args if not (runners_read and arg == "--slurp")], **kwargs)

    monkeypatch.setattr(ci_merge_queue_pool.subprocess, "run", without_slurp)
    with pytest.raises(MeasurementError, match="non-JSON"):
        measure("o/r", None)
