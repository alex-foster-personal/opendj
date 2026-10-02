"""Codex's grace: should the review chain wait for the Codex app on this head?

Absorbed from #4546's scripts/review_fallback.py (REVIEW-12) when the review
chain (REVIEW-15, scripts/review_chain.py) became the one fallback mechanism.

WHY. The Codex GitHub app is the only reviewer that runs by itself: it answers
every push, usually within minutes. A chain that ran Sol the moment coverage
read MISS would spend a subscription review on every fresh head before Codex
had a chance. So on a MISS the chain asks this module first:

  PROCEED  Codex posted its usage-limit notice AFTER the head was pushed, or
           stayed silent past CFG.CODEX_GRACE.
  WAIT     Codex has neither reviewed nor posted its notice, and the head was
           pushed less than CFG.CODEX_GRACE ago.

Whether Codex REVIEWED the head is not decided here: review coverage already
said MISS before this module is asked, and coverage is the one authority.

HOW A NOTICE IS TIED TO A HEAD. Codex's notice is an issue comment and carries
no SHA, so it is dated instead: it counts only when created at or after the
head's push time. The push time is the earliest check suite GitHub created for
the head SHA, which is when GitHub received the push. The commit's own
committer date is NOT used: it predates the push (PR #4538, commit 21:13:33Z,
suites 21:14:16Z, notice 21:14:22Z), and a notice for the previous head that
lands in that gap would otherwise be read as a notice for this one.

Requirements (mini-PRD):
  / A notice posted after the current head was pushed proceeds at once.
    [if] the notice sits 6 s after the push and the gate waits [then] broken
  / A notice left for an earlier head does not cut the grace short.
    [if] a notice dated before the push proceeds inside the grace [then] broken
  / Silence past the grace proceeds; silence inside it waits.
    [if] 19 min of silence proceeds, or 21 min waits [then] broken
  / A gh failure is a failed measurement, never "no notice found".
    [if] a gh error yields WAIT or PROCEED [then] broken
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from scripts.review_gh import TriageError, _gh, _paginated_json_list
from scripts.review_lane import REPO


class CFG:
    """Everything a reader needs to see to predict what the gate will say."""

    #: The Codex GitHub app's login, compared after stripping `[bot]`.
    CODEX_LOGIN: str = "chatgpt-codex-connector"
    #: Codex's own wording, captured verbatim on PR #4538 (Wed 30 Sep 2026).
    USAGE_LIMIT_NOTICE: str = "You have reached your Codex usage limits for code reviews"
    #: How long Codex may stay silent after a push before the chain runs a
    #: lane. Codex answered #4538 in 6 s and reviewed #4515 heads within 4 min.
    CODEX_GRACE: timedelta = timedelta(minutes=20)


class CodexGateError(TriageError):
    """The gate could not measure. Never rendered as WAIT or PROCEED."""


@dataclass(frozen=True)
class CodexArtifact:
    """One thing the Codex app left on the PR, in the shape `decide` reads."""

    kind: Literal["review", "comment"]
    at: datetime
    body: str
    #: The push a submitted review was left against; issue comments have none.
    commit_id: str | None = None


@dataclass(frozen=True)
class CodexGate:
    proceed: bool
    reason: str


# ----- the decision (pure) --------------------------------------------------


def _is_notice(artifact: CodexArtifact) -> bool:
    return CFG.USAGE_LIMIT_NOTICE.lower() in artifact.body.lower()


def _notice_at_head(
    head_sha: str, pushed: datetime, codex: tuple[CodexArtifact, ...]
) -> CodexArtifact | None:
    """A notice that answered THIS head: by SHA for a review, by date for a comment."""
    return next(
        (
            a
            for a in codex
            if _is_notice(a)
            and (a.commit_id == head_sha if a.kind == "review" else a.at >= pushed)
        ),
        None,
    )


def decide(
    head_sha: str, pushed: datetime, codex: tuple[CodexArtifact, ...], now: datetime
) -> CodexGate:
    """Wait for Codex, or proceed to the lanes. Pure: every input is an argument."""
    if now.tzinfo is None or pushed.tzinfo is None:
        raise CodexGateError("timestamps must be timezone-aware UTC")
    if pushed > now:
        raise CodexGateError(f"push time {pushed} is after now {now}; clock skew")
    short = head_sha[:10]
    notice = _notice_at_head(head_sha, pushed, codex)
    silent_for = now - pushed
    if notice is not None:
        return CodexGate(
            True, f"Codex posted its usage-limit notice at {notice.at:%a %d %b %H:%MZ} for {short}"
        )
    elif silent_for >= CFG.CODEX_GRACE:  # noqa: RET505 - outcomes stay explicit.
        return CodexGate(True, f"Codex silent {int(silent_for.total_seconds() // 60)} min after {short}")
    left = int((CFG.CODEX_GRACE - silent_for).total_seconds() // 60)
    return CodexGate(False, f"Codex has {left} min of grace left on {short}")


# ----- payload parsing (pure) -----------------------------------------------


def _utc(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(stamp)
    if parsed.tzinfo is None:
        raise CodexGateError(f"timestamp {stamp!r} carries no zone")
    return parsed.astimezone(UTC)


def _is_codex(payload: dict) -> bool:
    login = (payload.get("user") or {}).get("login", "")
    return login.removesuffix("[bot]").lower() == CFG.CODEX_LOGIN


def codex_artifacts(reviews: list[dict], comments: list[dict]) -> tuple[CodexArtifact, ...]:
    """Codex's reviews and issue comments, from GitHub's REST payload shapes."""
    found = [
        CodexArtifact("review", _utc(r["submitted_at"]), r.get("body") or "", r["commit_id"])
        for r in reviews
        if _is_codex(r)
    ]
    found += [
        CodexArtifact("comment", _utc(c["created_at"]), c.get("body") or "")
        for c in comments
        if _is_codex(c)
    ]
    return tuple(found)


def pushed_at_from_check_suites(pages: Iterable[dict]) -> datetime:
    """Earliest check suite GitHub created for the SHA: when the push arrived."""
    stamps = [_utc(s["created_at"]) for page in pages for s in page["check_suites"]]
    if not stamps:
        raise CodexGateError("no check suite exists for the head, so its push time is unknown")
    return min(stamps)


# ----- measurement (gh) -----------------------------------------------------


def codex_artifacts_on(pr: str) -> tuple[CodexArtifact, ...]:
    reviews = _paginated_json_list(f"repos/{REPO}/pulls/{pr}/reviews")
    comments = _paginated_json_list(f"repos/{REPO}/issues/{pr}/comments")
    return codex_artifacts(reviews, comments)


def pushed_at(sha: str) -> datetime:
    endpoint = f"repos/{REPO}/commits/{sha}/check-suites"
    return pushed_at_from_check_suites(json.loads(_gh(["api", endpoint, "--paginate", "--slurp"])))


def codex_gate(pr: str, head_sha: str) -> CodexGate:
    """The gate the chain asks on a coverage MISS. Raises TriageError when unmeasurable."""
    if len(head_sha) != 40:
        raise CodexGateError(f"head {head_sha!r} is not a full SHA, so its push cannot be dated")
    return decide(head_sha, pushed_at(head_sha), codex_artifacts_on(pr), datetime.now(UTC))
