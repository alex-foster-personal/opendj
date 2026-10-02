"""Subscription reviewer identities: what counts as a Grok or Cursor review artifact.

the maintainer's decision, 03:10Z Thu 1 Oct 2026 ("Auto failover chain"): one
`just review <PR>` tries Codex, Sol, then Grok and Cursor on the existing
subscriptions, then Claude last. Grok (SuperGrok Heavy) and Cursor (Cursor
Ultra, Composer house pool) are reached through the af-sub-broker `sb` CLI,
which bills the subscription and refuses metered spend unless told otherwise.
`scripts/subscription_review.py` is the lane that runs them; this module is
the SHAPE of their mark, and nothing else.

ONE MODULE, PARAMETERIZED BY LANE, where Sol and Claude each have their own
(`review_sol.py`, `review_claude.py`). Those two predate this one and differ
in real ways (Sol's marker carries a seat; Claude's does not). Grok and Cursor
differ only in their names and the `sb` provider/model they route to, so one
`SubscriptionLane` value per reviewer is the whole difference, and a third
subscription is one more entry in `SUBSCRIPTION_LANES`, not a third copy.

The writer/reader rule is the same as for Sol and Claude: `subscription_review`
WRITES the marker, `review_coverage`, `review_coverage_carry` and
`review_thread_parse` READ it, and the string is defined once, here.

IDENTITY TAKES TWO SIGNALS, AND NEITHER IS SUFFICIENT ALONE (see
`review_sol.py` for the full argument). The lane posts through `gh` as the maintainer,
so login alone would let any comment the maintainer writes satisfy coverage, and marker
alone would let any account that can comment forge a review. An artifact
counts only with BOTH, and the marker's SHA ties it to one push.

Requirements (mini-PRD):
  / A lane-written artifact at the current head is recognized.
    [if a real Grok or Cursor review cannot satisfy coverage then broken]
  / A marker pasted by anyone else is not recognized.
    [if a non-trusted login carrying a valid marker counts then broken]
  / A trusted login WITHOUT a marker is not recognized.
    [if the maintainer's own plain review comment counts as a Grok review then broken]
  / A marker naming an earlier push does not certify the current one.
    [if a stale-SHA marker counts at the current head then broken]
  / One lane's marker never certifies another lane.
    [if a grok-review marker counts as a Cursor review then broken]
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from scripts.review_prompt import CURSOR_FENCE, GROK_FENCE, Fence

#: Reviewer names as the coverage board prints them.
GROK = "Grok"
CURSOR = "Cursor"

#: Logins the lanes may post under. No bot account exists, so this is the
#: human account the lanes' `gh` token authenticates as; a future service
#: account is added here, never matched by loosening the marker.
SUBSCRIPTION_LOGINS: frozenset[str] = frozenset({"maintainer"})


def _normalize_login(login: str) -> str:
    return login.removesuffix("[bot]").lower()


@dataclass(frozen=True)
class SubscriptionLane:
    """One subscription reviewer: its board name, marker slug, fence and `sb` route."""

    name: str
    slug: str
    provider: str
    model: str
    fence: Fence

    @property
    def marker_re(self) -> re.Pattern[str]:
        """Anchored key order, so a hand-typed near-miss fails rather than half-counts."""
        return re.compile(
            rf"<!--\s*{re.escape(self.slug)}-review\s+v1\s+sha=([0-9a-f]{{7,40}})\s+model=(\S+)"
            r"(?:\s+skipped=([\w.,/-]+))?\s*-->",
            re.IGNORECASE,
        )

    def marker(self, sha: str, model: str, skipped: frozenset[str] = frozenset()) -> str:
        """The marker the lane writes into every artifact it posts."""
        tail = f" skipped={','.join(sorted(skipped))}" if skipped else ""
        return f"<!-- {self.slug}-review v1 sha={sha} model={model}{tail} -->"

    def skipped_paths_from_marker(self, body: str) -> frozenset[str]:
        match = self.marker_re.search(body or "")
        if not match or not match.group(3):
            return frozenset()
        return frozenset(match.group(3).split(","))

    def marker_skipped_mismatch(self, body: str, diff: str) -> str | None:
        from scripts.review_lane import skipped_paths_match_diff

        return skipped_paths_match_diff(self.skipped_paths_from_marker(body), diff)

    def is_artifact(self, login: str, body: str, head_sha: str) -> bool:
        """Written by this lane, against THIS push? Login, marker and SHA all required."""
        if _normalize_login(login) not in SUBSCRIPTION_LOGINS:
            return False
        match = self.marker_re.search(body or "")
        if not match:
            return False
        return head_sha.lower().startswith(match.group(1).lower())

    def is_thread(self, login: str, body: str) -> bool:
        """Opened by this lane at ANY head: authorship, for the three-state rule."""
        return _normalize_login(login) in SUBSCRIPTION_LOGINS and bool(
            self.marker_re.search(body or "")
        )


GROK_LANE = SubscriptionLane(GROK, "grok", "grok", "grok-4.6", GROK_FENCE)
CURSOR_LANE = SubscriptionLane(CURSOR, "cursor", "cursor", "composer-2.5", CURSOR_FENCE)

#: Chain order: Grok before Cursor, per the maintainer's decision.
SUBSCRIPTION_LANES: tuple[SubscriptionLane, ...] = (GROK_LANE, CURSOR_LANE)
LANES_BY_NAME: dict[str, SubscriptionLane] = {lane.name: lane for lane in SUBSCRIPTION_LANES}
LANES_BY_SLUG: dict[str, SubscriptionLane] = {lane.slug: lane for lane in SUBSCRIPTION_LANES}


def subscription_lane_of_thread(login: str, body: str) -> str:
    """The board name of the subscription lane that opened this thread, or ""."""
    return next((lane.name for lane in SUBSCRIPTION_LANES if lane.is_thread(login, body)), "")
