"""Codex-specific harness-at-head rules for REVIEW-13 control-plane dual review."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from scripts.review_gh import _SHA_IN_BACKTICKS, _STATUS_COMPLETED, _matches

if TYPE_CHECKING:
    from scripts.review_coverage import ReviewerEvidence

_CODE_REVIEW_ROW = re.compile(r"code\s*review", re.IGNORECASE)


def _codex_code_review_row_completed_at_head(body: str, head_sha: str) -> bool:
    """True when a summary table row names Code Review, the head, and Completed."""
    for line in body.splitlines():
        if not _CODE_REVIEW_ROW.search(line):
            continue
        shas = _SHA_IN_BACKTICKS.findall(line)
        if not shas:
            continue
        if not any(head_sha.lower().startswith(sha.lower()) for sha in shas):
            continue
        if _STATUS_COMPLETED.search(line):
            return True
    return False


def codex_head_tied_issue_comment(issue_comments: Sequence[Mapping[str, object]], head_sha: str) -> bool:
    """True when a Codex bot issue comment has a completed Code Review row at head."""
    for comment in issue_comments:
        user = comment.get("user")
        if isinstance(user, Mapping):
            login = str(user.get("login", ""))
        else:
            login = ""
        if not _matches(login, "Codex"):
            continue
        body = str(comment.get("body") or "")
        if _codex_code_review_row_completed_at_head(body, head_sha):
            return True
    return False


def harness_reviewed_at_head(
    name: str,
    evidence: ReviewerEvidence,
    reviewed: bool,
    issue_comments: Sequence[Mapping[str, object]],
    head_sha: str,
) -> bool:
    """Whether a harness counts at the current head for REVIEW-13's dual-review map.

    Codex, Sol and Claude use review_coverage._collect_evidence; Grok and Cursor
    are layered on separately. Sol, Claude, Grok and Cursor require a SUBMITTED
    review at head. Codex also accepts a head-tied clean-pass issue comment from
    its bot login when the summary table's Code Review row is Completed for this
    head (Security Review Completed alone does not count; fail-closed like
    review_gh._body_is_at_head line scoping).
    """
    if not reviewed:
        return False
    if evidence.submitted_reviews > 0:
        return True
    if name != "Codex":
        return False
    return codex_head_tied_issue_comment(issue_comments, head_sha)
