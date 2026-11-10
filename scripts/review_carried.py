"""Review-triage: BLOCKING findings a CLI lane carried in a review body (no thread).

Sol, Claude, Grok and Cursor put unanchored findings under `CARRIED_FINDINGS_HEADER`
when `split_by_anchor` cannot post inline. GitHub opens no review thread, so
thread-only triage never sees them (issue #4508). This module reads each lane's
latest submitted review and lists carried P0/P1 blockers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from scripts.review_claude import CLAUDE, is_claude_thread
from scripts.review_control_plane_carry import SUBMITTED_REVIEW_STATES
from scripts.review_gh import TriageError, _paginated_json_list
from scripts.review_lane import BLOCKING_SEVERITIES, CARRIED_FINDINGS_HEADER
from scripts.review_sol import SOL, is_sol_thread
from scripts.review_subscription import subscription_lane_of_thread

CARRIED_LINE = re.compile(
    r"^- (BLOCKING|NON-BLOCKING) (P[0-3]): (.+?) -- (.*)$"
)


@dataclass(frozen=True)
class CarriedFinding:
    verdict: str
    severity: str
    where: str
    title: str
    review_id: int
    lane: str
    submitted_at: str
    permalink: str

    @property
    def blocking(self) -> bool:
        return self.verdict == "BLOCKING" or self.severity in BLOCKING_SEVERITIES


@dataclass(frozen=True)
class CarriedFindingDraft:
    verdict: str
    severity: str
    where: str
    title: str


def lane_of(review: Mapping) -> str | None:
    """Board lane for this review, or None if unmarked or ambiguous."""
    login = (review.get("user") or {}).get("login", "")
    body = review.get("body") or ""
    matches: list[str] = []
    if is_sol_thread(login, body):
        matches.append(SOL)
    if is_claude_thread(login, body):
        matches.append(CLAUDE)
    sub = subscription_lane_of_thread(login, body)
    if sub:
        matches.append(sub)
    if len(matches) != 1:
        return None
    return matches[0]


def latest_lane_reviews(reviews: Sequence[Mapping]) -> dict[str, Mapping]:
    """Latest submitted review per CLI lane, by (submitted_at, id)."""
    best: dict[str, Mapping] = {}
    for review in reviews:
        state = str(review.get("state", "")).upper()
        if state not in SUBMITTED_REVIEW_STATES:
            continue
        lane = lane_of(review)
        if not lane:
            continue
        key = (str(review.get("submitted_at", "")), int(review["id"]))
        if lane not in best:
            best[lane] = review
            continue
        prior = best[lane]
        prior_key = (str(prior.get("submitted_at", "")), int(prior["id"]))
        if key > prior_key:
            best[lane] = review
    return best


def parse_carried(body: str) -> tuple[list[CarriedFindingDraft], bool]:
    """Findings under the carried header; second value True when malformed."""
    if CARRIED_FINDINGS_HEADER not in body:
        return [], False
    lines = body.splitlines()
    if CARRIED_FINDINGS_HEADER not in lines:
        return [], False
    start = lines.index(CARRIED_FINDINGS_HEADER) + 1
    found: list[CarriedFindingDraft] = []
    for line in lines[start:]:
        stripped = line.rstrip()
        if not stripped:
            continue
        if stripped.startswith("  "):
            continue
        if match := CARRIED_LINE.match(stripped):
            found.append(CarriedFindingDraft(*match.groups()))
            continue
        if stripped.startswith(("- BLOCKING", "- NON-BLOCKING")):
            return found, True
        break
    return found, False


def blocking_from(
    reviews: Sequence[Mapping], pr_url: str
) -> tuple[list[CarriedFinding], str | None]:
    """Union of blocking carried findings on each lane's latest review."""
    blocking: list[CarriedFinding] = []
    for lane, review in sorted(latest_lane_reviews(reviews).items()):
        drafts, malformed = parse_carried(review.get("body") or "")
        if malformed:
            return [], f"{lane} review {review['id']}: carried finding line is not parseable"
        rid = int(review["id"])
        link = f"{pr_url}#pullrequestreview-{rid}"
        submitted = str(review.get("submitted_at", ""))
        for draft in drafts:
            finding = CarriedFinding(
                draft.verdict,
                draft.severity,
                draft.where,
                draft.title,
                rid,
                lane,
                submitted,
                link,
            )
            if finding.blocking:
                blocking.append(finding)
    return blocking, None


def fetch_reviews(pr: int, owner: str, repo: str) -> list[dict]:
    """Every submitted review on the PR (paginated REST)."""
    try:
        return _paginated_json_list(f"repos/{owner}/{repo}/pulls/{pr}/reviews")
    except TriageError:
        raise
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        raise TriageError(f"could not list reviews for PR #{pr}: {exc}") from exc
