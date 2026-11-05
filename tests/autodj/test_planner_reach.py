"""AUTOPLAY-REACH: the planner's Beat Sync reach is the follower deck's real pitch range, 6% only as the default."""

from __future__ import annotations

from typing import Any

import pytest

from apps.autodj import propose as propose_module
from apps.autodj.live import heard_on_deck
from apps.autodj.plan import BEATMATCH_LIMIT_PCT, beat_sync_reach_pct, pair_beat_sync_fold, parse_plan
from apps.autodj.render_plan import layout_of
from apps.autodj.scoring import score_windows
from apps.autodj.windows import mix_windows
from tests.autodj.fixtures import profile

OUT = profile("out", bpm=128.0)
#: 12.3% under the master: the folded stretch the engine reached and locked on a 16% deck.
IN = profile("in", bpm=114.0)


def _score(in_: Any) -> Any:
    return score_windows(OUT, in_, mix_windows(OUT, "out", limit=1)[0], mix_windows(in_, "in", limit=1)[0])


@pytest.mark.requirement("AUTOPLAY-8870")
def test_a_16_percent_deck_reaches_a_12_percent_gap() -> None:
    """[if] the follower deck's pitch range is 16% [then] a 12% gap is within Beat Sync's reach, [else stop]."""
    on_deck = heard_on_deck(IN, {"stable_id": "in", "pitch_range": 16.0}, with_tempo=False)
    assert beat_sync_reach_pct(on_deck) == 16.0
    wide = _score(on_deck)
    assert wide.beatmatchable is True, "if a 16% deck reads a 12% gap as beyond reach then broken"
    assert any("within" in reason for reason in wide.reasons), "if the reason still says beyond then broken"


@pytest.mark.requirement("AUTOPLAY-8870")
def test_no_reported_range_keeps_the_6_percent_default() -> None:
    """[if] no deck reports a pitch range [then] the reach stays 6% and a 12% gap is beyond it, [else stop]."""
    assert beat_sync_reach_pct(IN) == BEATMATCH_LIMIT_PCT
    assert _score(IN).beatmatchable is False, "control: the default reach must still call 12% beyond"
    narrow = heard_on_deck(IN, {"stable_id": "in", "pitch_range": 0}, with_tempo=False)
    assert beat_sync_reach_pct(narrow) == BEATMATCH_LIMIT_PCT, "if a zero range is trusted as a reach then broken"


@pytest.mark.requirement("AUTOPLAY-8870b")
def test_the_proposal_folds_like_the_engine_on_a_16_percent_deck(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a 16% in-deck needs a double-time fold beyond 6% [then] the proposal uses the engine's fold, [else stop].

    So the incoming bass enters on the planned bar."""
    out, in_ = profile("monster", bpm=84.0), profile("sun", bpm=190.0)
    seen: list[float] = []
    real = propose_module.pair_beat_sync_fold

    def spy(out_bpm: Any, in_bpm: Any, range_pct: float = BEATMATCH_LIMIT_PCT) -> float:
        fold = real(out_bpm, in_bpm, range_pct)
        seen.append(fold)
        return fold

    monkeypatch.setattr(propose_module, "pair_beat_sync_fold", spy)
    live = {"decks": {"2": {"pitch_range": 16.0}}}
    assert pair_beat_sync_fold(84.0, 190.0, 16.0) == 2.0, "control: the engine folds 84 vs 190 at double time"
    assert pair_beat_sync_fold(84.0, 190.0) == 1.0, "control: the 6% default finds no fold"
    propose_module.propose_transition(out, in_, out_deck=1, in_deck=2, live=live)
    assert seen and seen[0] == 2.0, f"if the proposal lays out a fold other than the engine's 2 then broken: {seen}"
    assert propose_module._plan_bar(48, 0, 2.0) == 24.0, "an incoming bass at grid bar 48 lands on plan bar 24"


@pytest.mark.requirement("AUTOPLAY-8870b")
def test_the_offline_render_folds_as_the_proposal_does_at_128_over_72() -> None:
    """[if] 128 over 72 BPM plays on a 16% deck [then] the render folds at 0.5 as the proposal does, [else stop].

    The incoming tempo ratio is 0.8889, not the 1.7778 of fold 1."""
    out = profile("out128", bpm=128.0)
    on_deck = profile("in72", bpm=72.0).as_heard(pitch_range_pct=16.0)
    plans = propose_module.propose_transition(out, on_deck, out_deck=1, in_deck=2)["proposals"]
    plan = parse_plan(plans[0]["plan"])
    layout = layout_of(plan, out, on_deck, 4)
    assert layout.in_fold == 0.5, f"if the render folds {layout.in_fold}, not the proposal's 0.5, then broken"
    assert layout.in_ratio == pytest.approx(128.0 / 72.0 * 0.5, abs=1e-4)
    default = layout_of(plan, out, profile("in72", bpm=72.0), 4)
    assert default.in_fold == 1.0, "control: with no deck range the 6% default keeps fold 1"
