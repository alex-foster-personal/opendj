"""No re-review on a new push (the maintainer, 19:30Z Mon 2 Nov 2026: "no re-review required at all").

A review left on ANY earlier head of the PR covers every later head. Before this, coverage was
tied to the current head (with narrow carries for debt-only pushes and clean merges of main), so
every fix-up push drew a fresh Codex or Sol round and every lane waited on it. Open P0 and human
BLOCKING threads still block through review-triage's thread gate; this only drops the requirement
to review the new head again.

Recognition is review_coverage's own (`_collect_evidence` and `_classify_from_evidence` at the
earlier SHA), so who counts as a reviewer is still decided in one place. A bot that left no
recognized review artifact (Sentry, a usage-limit notice) counts at no head.

Requirements (mini-PRD):
  / A recognized review at an earlier head of this PR covers the current head.
    [if] Codex reviewed the previous push and a fix-up was pushed, and coverage reads MISS [then] broken
  / Only a recognized review counts.
    [if] a Sentry review or any unrecognized login covers a head [then] broken
  / An issue-comment-only review (a completed Codex summary row, a lane marker) counts too.
    [if] Codex's only artifact is a completed summary comment on the previous head and it does not cover [then] broken
  / Only a head of THIS PR counts. A review's commit_id or an inline original_commit_id is
    PR-scoped by GitHub and counts after a force-push; a SHA that is only body text must be a
    commit the PR holds now.
    [if] a marker or summary naming a SHA outside the PR's commits covers it [then] broken
    [if] a submitted review of a head later force-pushed away stops covering [then] broken
  / The current head is not "earlier".
    [if] a review at the current head is reported as an earlier-head carry [then] broken
"""

from __future__ import annotations

from collections.abc import Callable, Collection


def reviewed_at_an_earlier_head(
    reviews: list[dict],
    inline: list[dict],
    issue_comments: list[dict],
    head_sha: str,
    pr_commits: Callable[[], Collection[str]],
) -> tuple[str, str] | None:
    """(reviewer, sha) of a recognized review left on an earlier head of this PR.

    GitHub ties a submitted review's `commit_id` and an inline comment's `original_commit_id` to
    this PR when they are posted, so those count even after a force-push removed that head. A SHA
    that is only TEXT in a body (a lane marker, a Codex summary row) could name anything, so it
    counts only when it is a commit the PR holds now; `pr_commits` is read only for those."""
    from scripts import review_coverage as rc  # rc imports this module
    from scripts.review_coverage_carry import reviewed_shas_for_reviewer

    tied = {r.get("commit_id") for r in reviews} | {c.get("original_commit_id") for c in inline}
    named = {
        sha
        for name in rc.EXPECTED_REVIEWERS
        for sha in reviewed_shas_for_reviewer(name, reviews, inline, issue_comments, matches_login=rc._matches)
    }
    candidates = {s for s in (tied | named) if s and not head_sha.lower().startswith(s.lower())}
    if untied := candidates - tied:
        ours = [c.lower() for c in pr_commits()]
        candidates -= {s for s in untied if not any(c.startswith(s.lower()) for c in ours)}
    # Full SHAs first, so the PASS line names a whole commit when one is known.
    for sha in sorted(candidates, key=lambda s: (-len(s), s)):
        for name in rc.EXPECTED_REVIEWERS:
            evidence = rc._collect_evidence(name, reviews, inline, issue_comments, sha)
            if rc._classify_from_evidence(name, evidence).reviewed:
                return name, sha
    return None
