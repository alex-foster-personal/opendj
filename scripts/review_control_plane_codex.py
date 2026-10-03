"""Codex-specific harness-at-head rules for REVIEW-13 control-plane dual review."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from scripts.review_gh import _body_is_at_head, _matches

if TYPE_CHECKING:
    from scripts.review_coverage import ReviewerEvidence


def codex_head_tied_issue_comment(issue_comments: Sequence[Mapping[str, object]], head_sha: str) -> bool:
    """True when a Codex bot issue comment is head-tied the way _collect_evidence requires."""
    for comment in issue_comments:
        user = comment.get("user")
        if isinstance(user, Mapping):
            login = str(user.get("login", ""))
        else:
            login = ""
        if not _matches(login, "Codex"):
            continue
        body = str(comment.get("body") or "")
        if _body_is_at_head(body, head_sha):
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
    its bot login, using the same `_matches` normalization and `_body_is_at_head`
    tie as review_coverage._collect_evidence on raw issue payloads.
    """
    if not reviewed:
        return False
    if evidence.submitted_reviews > 0:
        return True
    if name != "Codex":
        return False
    return codex_head_tied_issue_comment(issue_comments, head_sha)
