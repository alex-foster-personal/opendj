"""PR scope check against declared issue limits (OPS-41 acceptance line 3, issue #3352).

[if] a PR exceeds its declared issue scope [then] the scope check fails with both counts.

Acceptance:
  - [if] a linked issue declares `Scope limit:` and the PR exceeds it [then] exit 1, both counts.
  - [if] the PR is within the declared limit [then] exit 0 with OK, never UNDECLARED.
  - [if] no linked issue declares a limit [then] exit 0 as UNDECLARED, OVERSIZED past the bound.
  - [if] review-triage runs [then] it runs the scope check and its failure fails triage.
"""

from __future__ import annotations

import inspect

import pytest

from scripts import pr_scope_check as psc
from scripts import review_thread_triage
from scripts.worker_worktree_guard import Scope

pytestmark = pytest.mark.requirement("OPS-41")

# The #3352 incident against its stated base: 118 commits, 131 files, for one commit of work.
INCIDENT = Scope(commits=118, files=131, additions=13000, deletions=40)
SMALL = Scope(commits=1, files=3, additions=40, deletions=2)


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


def test_review_triage_runs_the_scope_check_and_propagates_its_failure() -> None:
    source = inspect.getsource(review_thread_triage.main)
    assert "pr_scope_check.main(" in source
    assert "max(scope_rc," in source
