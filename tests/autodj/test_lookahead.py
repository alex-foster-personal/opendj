"""AUTOPLAN-02, AUTOPLAN-03: the search ahead in the master and the shortlist's chooser, on real profiles."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from apps.autodj import lookahead
from apps.autodj.lookahead import _peak_within, decide, windows_ahead
from apps.autodj.profile import profile_from_payloads
from apps.autodj.propose import _PREFERENCES, parse_intent, propose_transition
from tests.autodj.fixtures import CHORUS as CHORUS_KIND
from tests.autodj.fixtures import DOWN, INTRO, OUTRO, UP, anlz_payload, profile

OUT, IN = profile("trk-out"), profile("trk-in", key="9A")
# Default arrangement bars: INTRO 1, UP 17, CHORUS 33, DOWN 65, UP 81, CHORUS 97, OUTRO 113, 128 bars in all.


def _mirror() -> dict[str, Any]:
    channel = {"fader": 1.0, "trim": 0.5, "eq_low": 0.5, "eq_mid": 0.5, "eq_high": 0.5, "filter": 0.5, "assign": "thru"}
    deck = {
        "playing": False,
        "bpm": 128.0,
        "effective_bpm": 128.0,
        "pitch": 1.0,
        "pitch_range": 8.0,
        "quantized_launch_armed": None,
        "sync": {"mode": "off", "enabled": False},
    }
    return {
        "master_deck": 1,
        "decks": {
            "1": {**deck, "stable_id": "trk-out", "playing": True, "position": {"ms": OUT.time_of_bar(40) * 1000.0}},
            "2": {**deck, "stable_id": None, "position": {"ms": 0.0}},
        },
        "mixer": {"crossfader": 0.5, "channels": {"1": dict(channel), "2": {**channel, "fader": 0.0}}},
    }


def _decide(shortlist: list[Any], *, intent: str | None = "smooth", recent: tuple[str, ...] = ()) -> Any:
    return decide(
        OUT, shortlist, decks=(1, 2), intent=intent, recent_styles=recent, earliest_bar=48.0, target_bar=50.0, live=_mirror()
    )


def test_only_phrase_starts_past_the_earliest_bar_are_offered() -> None:
    starts = {section.start_bar for section in OUT.sections}
    windows = windows_ahead(OUT, earliest_bar=64.0, target_bar=64.0)
    assert windows, "if the search finds no windows in a phrased track then broken"
    assert {window.start_bar for window in windows} <= starts, "if a window is off a phrase start then broken"
    assert all(window.start_bar > 64 for window in windows), "if a window is behind the earliest bar then broken"


def test_no_phrase_analysis_means_no_windows() -> None:
    assert windows_ahead(replace(OUT, sections=()), earliest_bar=1.0, target_bar=1.0) == [], "if a boundary is invented then broken"


def test_the_breakdown_beats_the_distant_outro_when_the_target_is_near_it() -> None:
    best = windows_ahead(OUT, earliest_bar=60.0, target_bar=64.0)[0]
    assert best.start_bar == 65, f"if the soft target is ignored then broken, got bar {best.start_bar}"


def test_the_outro_wins_when_the_target_is_already_near_it() -> None:
    best = windows_ahead(OUT, earliest_bar=105.0, target_bar=110.0)[0]
    assert best.start_bar == 113, f"if the outro loses near its own start then broken, got bar {best.start_bar}"


def test_a_phrase_start_right_before_a_chorus_is_penalized() -> None:
    arrangement = ((INTRO, 16, 0.4, 0.2, False), (UP, 8, 0.6, 0.5, False), (CHORUS_KIND, 32, 1.0, 1.0, False), (OUTRO, 16, 0.4, 0.8, False))
    track = profile_from_payloads(anlz_payload("guard", arrangement=arrangement), {"stable_id": "guard"})
    assert _peak_within(track, 17), "if a chorus 8 bars ahead is not seen then broken"
    assert _peak_within(track, 25), "if a window right on the chorus counts as clear then broken"
    assert not _peak_within(track, 1), "if a chorus 24 bars ahead counts as close then broken"


def test_a_breakdown_beats_a_nearer_chorus_in_a_track_of_drops() -> None:
    """Live on a real track, Fri 30 Oct 2026: every phrase is a drop bar one breakdown, and only the search's reach hid it."""
    arrangement = ((INTRO, 16, 0.4, 0.2, False), (CHORUS_KIND, 16, 1.0, 1.0, False), (DOWN, 16, 0.3, 0.1, False), (CHORUS_KIND, 32, 1.0, 1.0, False))
    track = profile_from_payloads(anlz_payload("drops", arrangement=arrangement), {"stable_id": "drops"})
    best = windows_ahead(track, earliest_bar=16.0, target_bar=16.0)[0]
    assert best.start_bar == 33, f"if a phrase on a drop beats the breakdown after it then broken, got bar {best.start_bar}"


def test_decide_answers_a_track_and_a_window_on_a_phrase() -> None:
    decision = _decide([IN])
    assert decision.track == "trk-in", f"if a plannable track is not chosen then broken: {decision.reason}"
    assert decision.window is not None and decision.window.start_bar > 48
    assert decision.window.start_bar in {section.start_bar for section in OUT.sections}


def test_the_chooser_picks_by_intent_not_by_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """The proposal score already leans toward the intent, so it is inverted here: only the chooser's order can pass."""
    order = [name for name, _bars in _PREFERENCES[parse_intent("smooth")]]

    def inverted(*args: Any, **kwargs: Any) -> dict[str, Any]:
        document = propose_transition(*args, **kwargs)
        rows = document["proposals"]
        return {**document, "proposals": [{**row, "score": 0.5 + 0.01 * order.index(row["style"])} for row in rows]}

    monkeypatch.setattr(lookahead, "propose_transition", inverted)
    rows = inverted(OUT, IN, out_deck=1, in_deck=2, intent="smooth", min_out_bar=48.0, live=_mirror(), top=None)["proposals"]
    styles = {row["style"] for row in rows}
    assert len(styles) > 1, "control: one style cannot tell intent from score"
    assert max(rows, key=lambda row: row["score"])["style"] != min(styles, key=order.index), "control: the two must disagree"
    assert _decide([IN]).style == min(styles, key=order.index), "if the score beat the intent's order then broken"


def test_a_repeated_style_sits_out_while_another_is_eligible() -> None:
    first = _decide([IN])
    assert first.style is not None
    second = _decide([IN], recent=(first.style,))
    assert second.style != first.style, "if variety is ignored across the shortlist then broken"


def test_no_valid_plan_is_an_answer_naming_what_was_tried() -> None:
    decision = decide(
        OUT, [IN], decks=(1, 2), intent="smooth", recent_styles=(), earliest_bar=127.0, target_bar=127.0, live=_mirror()
    )
    assert decision.track is None, "if a plan is invented past the last phrase then broken"
    assert decision.reason, "if no reason is given then broken"
