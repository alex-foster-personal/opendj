"""Choosing among rendered candidates: M is a floor, not the objective (D16, ADR-0130).

M (apps.transition_score) measures hygiene: alignment, loudness continuity, bass overlap, clicks. A chooser that
maximizes it converges on the blandest hygiene-maximal shape, which is a crossfade (the maintainer, Thu 29 Oct 2026: "Have a bad
feeling our rules will now just converge towards all sounding like crossfades"). So M only gates eligibility; among the
eligible the intent decides, then variety, and M breaks ties.

Requirements:
  ✔︎ a vetoed candidate or one under the floor is never chosen while an eligible one exists
  ✔︎ among eligible candidates the intent's own style order wins over M
  ✔︎ the previous transition's style is not repeated when another eligible style exists
  ✔︎ in a running set a style plays at most STYLE_CAP times in VARIETY_WINDOW transitions while another is eligible
Acceptance:
  [if] an energy_up pair has an eligible cut and a higher-M crossfade [then] the cut is chosen
  [if] the only eligible candidate repeats the previous style [then] it is still chosen, never nothing
  [if] every candidate is vetoed or under the floor [then ⛔️] None, never a guess
  [if] every eligible style is at the cap [then] the intent's first eligible style still plays, never nothing
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from apps.autodj.contract.models import STYLE_CAP, VARIETY_WINDOW
from apps.autodj.contract.models import ChooserCandidate as Candidate
from apps.autodj.propose import _PREFERENCES, parse_intent

#: Provisional: a candidate is eligible at 0.6 of the pair's best unvetoed M. Relative, because M on real pairs spans
#: 0 to about 0.45 with five of seven factors measured, so no absolute value is comparable across pairs yet. Not fitted
#: to the maintainer's verdicts (they steer direction only); revisit once M measures all seven factors.
FLOOR_SHARE = 0.6
def eligible(candidates: list[Candidate]) -> list[Candidate]:
    """The unvetoed candidates at or above FLOOR_SHARE of the best unvetoed M."""
    clean = [c for c in candidates if not c.vetoed]
    if not clean:
        return []
    floor = FLOOR_SHARE * max(c.m for c in clean)
    return [c for c in clean if c.m >= floor]


def choose(candidates: list[Candidate], intent: str | None, previous_style: str | None = None) -> Candidate | None:
    """Intent's style order decides among the eligible, skipping the previous style while any other is eligible; M
    breaks ties within one style."""
    pool = eligible(candidates)
    if not pool:
        return None
    order = [name for name, _bars in _PREFERENCES[parse_intent(intent)]]

    def key(candidate: Candidate) -> tuple[int, float]:
        return (order.index(candidate.style) if candidate.style in order else len(order), -candidate.m)

    fresh = [c for c in pool if c.style != previous_style] or pool
    return min(fresh, key=key)


def choose_for_set(candidates: list[Candidate], intent: str | None, recent_styles: Sequence[str]) -> Candidate | None:
    """:func:`choose` inside a running set: styles at STYLE_CAP in the last VARIETY_WINDOW transitions sit out, as on
    the bench. When that empties the pool the cap is dropped, because a repeated plan beats no plan.

    The cap filters BEFORE the floor, as the bench's round 22 set did (the maintainer heard those picks), so a capped style's M
    does not set the floor for the rest. Changing that order changes what he approved."""
    window = list(recent_styles)[-VARIETY_WINDOW:]
    played = Counter(window)
    previous = window[-1] if window else None
    fresh = [c for c in candidates if played[c.style] < STYLE_CAP]
    return choose(fresh, intent, previous) or choose(candidates, intent, previous)
