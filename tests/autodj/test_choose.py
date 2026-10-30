"""The chooser (D16): M gates eligibility; intent, then variety, then M decide."""

from __future__ import annotations

import pytest

from apps.autodj.choose import FLOOR_SHARE, STYLE_CAP, VARIETY_WINDOW, Candidate, choose, choose_for_set, eligible


def _pick(pool: list[Candidate], intent: str, previous_style: str | None = None) -> str:
    chosen = choose(pool, intent, previous_style)
    assert chosen is not None
    return chosen.cid


def _c(cid: str, style: str, m: float, vetoed: bool = False) -> Candidate:
    return Candidate(cid, style, m, vetoed)


@pytest.mark.requirement("AGENT-43")
def test_intent_beats_a_higher_m_crossfade() -> None:
    """[if] an energy_up pair has an eligible cut and a higher-M crossfade [then] the cut is chosen, [else stop]."""
    pool = [_c("a", "crossfade", 0.40), _c("b", "cut", 0.30)]
    assert _pick(pool, "energy_up") == "b"
    # Control: under the floor the sharp style is not eligible, so M's pick stands.
    low = [_c("a", "crossfade", 0.40), _c("b", "cut", 0.40 * FLOOR_SHARE - 0.01)]
    assert _pick(low, "energy_up") == "a"


@pytest.mark.requirement("AGENT-43")
def test_a_vetoed_candidate_is_never_chosen_and_none_left_is_none() -> None:
    """[if] a candidate is vetoed [then] it is never chosen; with nothing eligible the answer is None, [else stop]."""
    assert _pick([_c("a", "cut", 0.9, vetoed=True), _c("b", "crossfade", 0.2)], "energy_up") == "b"
    assert choose([_c("a", "cut", 0.9, vetoed=True)], "energy_up") is None
    assert eligible([]) == []


@pytest.mark.requirement("AGENT-43")
def test_the_previous_style_is_skipped_unless_it_is_all_there_is() -> None:
    """[if] the previous transition used the first choice [then] the next eligible style wins, [else stop].

    A lone repeat is still chosen, never nothing."""
    pool = [_c("a", "drop_swap", 0.3), _c("b", "cut", 0.3)]
    assert _pick(pool, "showy") == "a"
    assert _pick(pool, "showy", previous_style="drop_swap") == "b"
    assert _pick([_c("a", "drop_swap", 0.3)], "showy", previous_style="drop_swap") == "a"


@pytest.mark.requirement("AGENT-48")
def test_a_style_at_the_cap_sits_out_within_the_window_only() -> None:
    """[if] a style played STYLE_CAP times in the window [then] the next eligible style wins, and past the window it is back, [else stop]."""
    pool = [_c("a", "long_blend", 0.4), _c("b", "crossfade", 0.4)]
    capped = choose_for_set(pool, "smooth", ["long_blend", "wave", "long_blend", "melt"])
    assert capped is not None and capped.cid == "b"
    aged = ["long_blend", "long_blend", *["wave", "melt", "rise", "wave", "melt", "rise", "cut", "drop_swap", "cut", "melt"]]
    assert len(aged) - VARIETY_WINDOW == STYLE_CAP
    fresh = choose_for_set(pool, "smooth", aged)
    assert fresh is not None and fresh.cid == "a"


@pytest.mark.requirement("AGENT-48")
def test_a_capped_pool_still_plays_rather_than_nothing() -> None:
    """[if] every eligible style is at the cap [then] the intent's first eligible style plays, never nothing, [else stop]."""
    pool = [_c("a", "long_blend", 0.4), _c("b", "crossfade", 0.4)]
    chosen = choose_for_set(pool, "smooth", ["long_blend", "crossfade", "long_blend", "crossfade"])
    assert chosen is not None and chosen.cid == "a"
    # Control: the previous style is still skipped when the cap is dropped.
    chosen = choose_for_set(pool, "smooth", ["crossfade", "long_blend", "crossfade", "long_blend"])
    assert chosen is not None and chosen.cid == "b"
    assert choose_for_set([_c("a", "cut", 0.9, vetoed=True)], "smooth", ["cut", "cut"]) is None
