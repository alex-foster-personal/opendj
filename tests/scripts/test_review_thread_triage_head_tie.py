"""The thread snapshot must belong to the head that coverage certified.

PR #1053 P1 BLOCKING (chatgpt-codex-connector thread r3929980333, Thu 3 Sep
2026): `main()` ran `review_coverage.triage()` (which re-checks its own head)
and THEN fetched review threads over a further GraphQL call that carried no
head SHA. A push landing between those two calls left `coverage` describing
the old SHA while `pr` described the new, unreviewed one; a clean thread
snapshot then exited 0. The fix samples the head before coverage, includes
`headRefOid` in the thread query, and voids the run (exit 3, a failed
measurement) when the two disagree.
"""

from __future__ import annotations

import pytest

from scripts import review_coverage, review_thread_triage
from scripts.review_thread_parse import PullRequest

_OLD = "884ca47aa11f2b3c4d5e6f708192a3b4c5d6e7f0"
_NEW = "7cbe7496d259f8f88c67e409d497f48670db9c10"


def _pr(head_sha: str) -> PullRequest:
    return PullRequest(
        number=1053,
        title="t",
        state="OPEN",
        merged=False,
        url="https://github.com/private_owner/music-dj-tools/pull/1053",
        head_sha=head_sha,
        threads=(),
    )


def _run(monkeypatch: pytest.MonkeyPatch, sampled: str, fetched: str) -> int:
    monkeypatch.setattr(review_coverage, "_head_sha", lambda pr: sampled)
    monkeypatch.setattr(review_coverage, "triage", lambda pr: 0)
    monkeypatch.setattr(
        review_thread_triage, "fetch_pull_request", lambda n, owner, repo: _pr(fetched)
    )
    monkeypatch.setattr(
        review_thread_triage,
        "check_pr_head_debt_file",
        lambda number, head_sha, owner="", repo="": (None, []),
    )
    # The OPS-41 scope step has its own tests (test_pr_scope_check.py); here it
    # must not reach the live API, and must not decide the head-tie verdict.
    monkeypatch.setattr(review_thread_triage.pr_scope_check, "main", lambda argv: 0)
    monkeypatch.setattr("sys.argv", ["review_thread_triage", "1053"])
    return review_thread_triage.main()


def test_a_push_between_coverage_and_the_thread_fetch_voids_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE CASE THE P1 NAMED: zero threads at a head coverage never measured
    must be a failed measurement (3), never a pass (0)."""
    assert _run(monkeypatch, sampled=_OLD, fetched=_NEW) == 3


def test_an_unchanged_head_still_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: the guard must not fire when nothing moved, or the test above
    would pass by failing every run."""
    assert _run(monkeypatch, sampled=_NEW, fetched=_NEW) == 0


def test_the_thread_query_carries_the_head_sha() -> None:
    """The fetched snapshot can only be tied to a head if the query asks for one."""
    assert "headRefOid" in review_thread_triage._THREADS_QUERY
