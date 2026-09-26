"""PR scope check against declared issue limits (OPS-41 acceptance line 3, issue #3352).

[if] a PR exceeds its declared issue scope [then] the scope check fails with both counts.

Acceptance:
  - [if] a linked issue declares `Scope limit:` and the PR exceeds it [then] exit 1, both counts.
  - [if] the PR is within the declared limit [then] exit 0 with OK, never UNDECLARED.
  - [if] no linked issue declares a limit [then] exit 0 as UNDECLARED, OVERSIZED past the bound.
  - [if] review-triage runs [then] it runs the scope check and its exit decides triage.
  - [if] the linked issues cannot all be read [then] exit 3, never UNDECLARED.
"""

from __future__ import annotations

import pytest

from scripts import pr_scope_check as psc
from scripts import review_coverage, review_thread_triage
from scripts.review_thread_parse import PullRequest
from scripts.worker_worktree_guard import Scope

pytestmark = pytest.mark.requirement("OPS-41")

# The #3352 incident against its stated base: 118 commits, 131 files, for one commit of work.
INCIDENT = Scope(commits=118, files=131, additions=13000, deletions=40)
SMALL = Scope(commits=1, files=3, additions=40, deletions=2)
_HEAD = "7cbe7496d259f8f88c67e409d497f48670db9c10"


def test_declared_limit_exceeded_fails_and_prints_both_measurements() -> None:
    limit = psc.declared_limit({3307: "Fix it.\n\nScope limit: commits=5 files=20\n"})
    rc, line = psc.verdict(INCIDENT, limit)
    assert rc == 1
    assert "commits=118 (max 5)" in line and "files=131 (max 20)" in line
    assert "declared by #3307" in line


def test_within_declared_limit_passes_as_ok() -> None:
    rc, line = psc.verdict(SMALL, psc.declared_limit({1: "Scope limit: commits=5, files=20"}))
    assert rc == 0 and line.startswith("[pr-scope] OK:")


def test_boundary_equal_to_the_limit_passes() -> None:
    """Control for the overshoot: the limit is a maximum, so equal must pass."""
    rc, _ = psc.verdict(
        Scope(5, 20, 0, 0), psc.declared_limit({1: "Scope limit: commits=5 files=20"})
    )
    assert rc == 0


def test_one_axis_over_is_enough_to_fail() -> None:
    limit = psc.declared_limit({1: "scope-limit: commits=50 files=4"})
    assert psc.verdict(SMALL, limit)[0] == 0
    assert psc.verdict(Scope(2, 5, 0, 0), limit)[0] == 1


def test_tightest_declaration_wins_across_linked_issues() -> None:
    limit = psc.declared_limit(
        {1: "Scope limit: commits=10 files=5", 2: "Scope limit: commits=3 files=50"}
    )
    assert limit is not None
    assert (limit.commits, limit.files, limit.issues) == (3, 5, (1, 2))


def test_undeclared_is_never_printed_as_ok() -> None:
    rc, line = psc.verdict(SMALL, psc.declared_limit({1: "no declaration here", 2: ""}))
    assert rc == 0
    assert line.startswith("[pr-scope] UNDECLARED:") and "OVERSIZED" not in line


def test_undeclared_incident_is_surfaced_as_oversized() -> None:
    rc, line = psc.verdict(INCIDENT, None)
    assert rc == 0
    assert "OVERSIZED" in line and "commits=118" in line and "files=131" in line


def test_refs_parser_reads_the_issue_link_forms_prs_use() -> None:
    body = "Refs #3352. Fixes #10 and closes #11; resolves #12. Not a ref: PR#99"
    assert sorted(int(n) for n in psc._REFS_RE.findall(body)) == [10, 11, 12, 3352]


# -- fetch: a partial or unresolvable read is no verdict ----------------------------


def _fake_graphql(pr: dict, refs: dict[int, dict | None]):
    def graphql(query: str, **variables: object) -> dict:
        if "pullRequest(number" in query:
            return {"pullRequest": pr}
        return {"issueOrPullRequest": refs[int(variables["n"])]}  # type: ignore[call-overload]

    return graphql


def _pr_payload(body: str, closing: list[dict], total: int | None = None) -> dict:
    return {
        "headRefOid": "0" * 40,
        "body": body,
        "commits": {"totalCount": 2},
        "changedFiles": 3,
        "additions": 10,
        "deletions": 1,
        "closingIssuesReferences": {
            "totalCount": len(closing) if total is None else total,
            "nodes": closing,
        },
    }


def test_truncated_closing_issue_list_is_a_measure_error_not_undeclared() -> None:
    pr = _pr_payload("", [{"number": 1, "body": ""}], total=51)
    with pytest.raises(psc.MeasureError, match="links 51 closing issues, read 1"):
        psc.fetch(9, "o", "r", graphql=_fake_graphql(pr, {}))


def test_refs_to_a_pull_request_are_skipped_and_issue_limits_still_read() -> None:
    pr = _pr_payload("Refs #3945. Refs #3352.", [])
    refs: dict[int, dict | None] = {
        3945: {"__typename": "PullRequest"},
        3352: {"__typename": "Issue", "number": 3352, "body": "Scope limit: commits=1 files=2"},
    }
    scope, bodies = psc.fetch(9, "o", "r", graphql=_fake_graphql(pr, refs))
    assert scope == Scope(2, 3, 10, 1)
    assert set(bodies) == {3352}
    assert psc.verdict(scope, psc.declared_limit(bodies))[0] == 1


def test_an_unresolvable_ref_is_a_measure_error() -> None:
    pr = _pr_payload("Fixes #77", [])
    with pytest.raises(psc.MeasureError, match="#77, which does not resolve"):
        psc.fetch(9, "o", "r", graphql=_fake_graphql(pr, {77: None}))


# -- review-triage integration: the scope exit decides triage ----------------------


def _pr_clean() -> PullRequest:
    return PullRequest(
        number=9,
        title="t",
        state="OPEN",
        merged=False,
        url="https://github.com/maintainer/music-dj-tools/pull/9",
        head_sha=_HEAD,
        threads=(),
    )


def _triage_with_scope_rc(
    monkeypatch: pytest.MonkeyPatch, scope_rc: int
) -> tuple[int, list[list[str]]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(review_coverage, "_head_sha", lambda pr: _HEAD)
    monkeypatch.setattr(review_coverage, "triage", lambda pr: 0)
    monkeypatch.setattr(
        review_thread_triage, "fetch_pull_request", lambda n, owner, repo: _pr_clean()
    )
    monkeypatch.setattr(
        review_thread_triage,
        "check_pr_head_debt_file",
        lambda number, head_sha, owner="", repo="": (None, []),
    )

    def fake_scope_main(argv: list[str]) -> int:
        calls.append(argv)
        return scope_rc

    monkeypatch.setattr(psc, "main", fake_scope_main)
    monkeypatch.setattr("sys.argv", ["review_thread_triage", "9"])
    return review_thread_triage.main(), calls


def test_triage_passes_when_threads_and_scope_are_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: a clean PR with a clean scope must pass, or the tests below prove nothing."""
    rc, calls = _triage_with_scope_rc(monkeypatch, 0)
    assert rc == 0 and calls and calls[0][0] == "9"


def test_triage_fails_when_the_scope_check_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _triage_with_scope_rc(monkeypatch, 1)[0] == 1


def test_triage_reports_unmeasured_when_the_scope_cannot_be_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _triage_with_scope_rc(monkeypatch, 3)[0] == 3
